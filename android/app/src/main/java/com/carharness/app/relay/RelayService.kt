package com.carharness.app.relay

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import com.carharness.app.MainActivity
import com.carharness.app.R
import com.carharness.app.obd.ObdTransport
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * Relay mode: this phone becomes the desktop's dongle.
 *
 * A foreground service and not a coroutine in the Activity, because the job is
 * to sit in a car with the screen off for as long as the diagnostic takes. The
 * notification is the honest price of that, and it says what is being relayed
 * where.
 *
 * The link itself is [RelayLink]; everything here is Android plumbing around
 * it: one WebSocket, a reconnect with backoff while relay is wanted, a keepalive
 * ping, and a single worker thread so the dongle round-trip never blocks
 * OkHttp's reader.
 */
class RelayService : Service() {

    companion object {
        private const val CHANNEL_ID = "car-harness-relay"
        private const val NOTIFICATION_ID = 4201
        private const val EXTRA_URL = "url"
        private const val EXTRA_DESKTOP = "desktop"
        private const val EXTRA_DONGLE = "dongle"
        private const val PING_SECONDS = 20L
        private const val MAX_BACKOFF_SECONDS = 30L

        /** The live dongle link, lent to the relay by AppState. While relay is
         *  on this is the ONLY thing allowed to drive it — the phone's own scan
         *  and live-data actions are gated off for the duration. */
        @Volatile
        var transport: ObdTransport? = null

        /** Status line for the UI ("Relaying — 42 commands", "Dropped: …"). */
        @Volatile
        var onStatus: ((String) -> Unit)? = null

        /** Fired when the service gives up on its own (no dongle to relay). */
        @Volatile
        var onStopped: (() -> Unit)? = null

        @Volatile
        var running = false
            private set

        fun start(context: Context, url: String, desktopName: String, dongleName: String) {
            val intent = Intent(context, RelayService::class.java)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_DESKTOP, desktopName)
                .putExtra(EXTRA_DONGLE, dongleName)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, RelayService::class.java))
        }
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS) // a relay is idle between commands
        .build()

    /** The dongle round-trip runs here, one at a time, off OkHttp's reader. */
    private val worker = Executors.newSingleThreadExecutor()
    private val timers = Executors.newSingleThreadScheduledExecutor()

    private var url = ""
    private var desktopName = ""
    private var dongleName = ""

    @Volatile private var wanted = true
    @Volatile private var socket: WebSocket? = null
    @Volatile private var link: RelayLink? = null
    private var pingTask: ScheduledFuture<*>? = null
    private var attempt = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent != null) {
            url = intent.getStringExtra(EXTRA_URL) ?: url
            desktopName = intent.getStringExtra(EXTRA_DESKTOP) ?: desktopName
            dongleName = intent.getStringExtra(EXTRA_DONGLE) ?: dongleName
        }
        if (running) return START_NOT_STICKY // already relaying; don't stack links
        running = true
        wanted = true
        startForegroundNotice()
        connect()
        // Not sticky: a relay restarted without its dongle would be a service
        // holding a notification over nothing.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        wanted = false
        running = false
        pingTask?.cancel(false)
        try {
            socket?.close(1000, "Relay switched off")
        } catch (_: Exception) {
        }
        socket = null
        link = null
        timers.shutdownNow()
        worker.shutdownNow()
        client.dispatcher.executorService.shutdown()
        super.onDestroy()
    }

    // ------------------------------------------------------------ the socket
    private fun connect() {
        if (!wanted) return
        val dongle = transport ?: run {
            report("No dongle connected — relay stopped.")
            onStopped?.invoke()
            stopSelf()
            return
        }
        report(if (attempt == 0) "Connecting to $desktopName…" else "Reconnecting…")
        val request = try {
            Request.Builder().url(url).build()
        } catch (e: Exception) {
            report("That desktop address can't be reached: ${e.message}")
            onStopped?.invoke()
            stopSelf()
            return
        }
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                attempt = 0
                val relay = RelayLink(
                    transport = dongle,
                    socket = object : RelaySocket {
                        override fun send(text: String): Boolean = webSocket.send(text)
                    },
                    worker = worker,
                    onProgress = { n -> report("Relaying — $n commands") },
                )
                link = relay
                relay.hello(dongleName)
                report("Relaying to $desktopName")
                pingTask?.cancel(false)
                pingTask = timers.scheduleWithFixedDelay(
                    { link?.ping() }, PING_SECONDS, PING_SECONDS, TimeUnit.SECONDS,
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                link?.onText(text)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                // 4401 (not paired) / 4409 (another phone already relaying)
                // arrive here as a plain failure; the server's reason is the
                // most useful thing we can show.
                retryLater(response?.message?.ifBlank { null } ?: t.message ?: "link failed")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                retryLater(reason.ifBlank { "the desktop closed the link" })
            }
        })
    }

    private fun retryLater(reason: String) {
        pingTask?.cancel(false)
        link = null
        if (!wanted) return
        attempt += 1
        // 2s, 4s, 8s, 16s, then 30s for as long as the car sits there.
        val wait = minOf(1L shl minOf(attempt, 5), MAX_BACKOFF_SECONDS)
        report("Dropped: $reason — retrying in ${wait}s")
        try {
            timers.schedule({ connect() }, wait, TimeUnit.SECONDS)
        } catch (_: Exception) {
            // The service is going down; nothing left to retry onto.
        }
    }

    private fun report(text: String) {
        onStatus?.invoke(text)
    }

    // ------------------------------------------------------- the notification
    private fun startForegroundNotice() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Relay", NotificationManager.IMPORTANCE_LOW,
            )
            channel.description = "Shown while this phone is relaying its dongle to the desktop."
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, notification(),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            } else {
                0
            },
        )
    }

    private fun notification(): Notification {
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags = flags or PendingIntent.FLAG_IMMUTABLE
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), flags,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Car Harness relay")
            .setContentText(
                "Relaying ${dongleName.ifBlank { "the dongle" }} to " +
                    desktopName.ifBlank { "the desktop" }
            )
            .setSmallIcon(R.mipmap.ic_launcher)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }
}
