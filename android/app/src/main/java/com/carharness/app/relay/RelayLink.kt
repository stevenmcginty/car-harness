package com.carharness.app.relay

import com.carharness.app.obd.ObdTransport
import org.json.JSONObject
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * The relay's one job, with no Android in it.
 *
 * The desktop is at home, the car is here, and the phone is the wire between
 * them. A frame comes in carrying raw ELM327 bytes, those bytes go to the
 * dongle, and whatever the dongle says goes back out as one frame. The phone
 * decodes nothing and decides nothing — every bit of protocol knowledge stays
 * where it already lives, on the desktop.
 *
 * Wire protocol (both directions), matching backend/obd/remote_link.py:
 *   {"type":"data","b64":"<base64 raw ELM bytes>"}
 * plus three the server ignores but a human reading a log will not:
 *   {"type":"hello","device":"..."}  {"type":"ping"}  {"type":"error","detail":"..."}
 */
class RelayLink(
    private val transport: ObdTransport,
    private val socket: RelaySocket,
    /** Where the blocking dongle exchange runs. Never the socket's own reader
     *  thread: a slow dongle would stall the frames behind it. */
    private val worker: Executor,
    private val exchangeTimeoutMs: Long = 10_000,
    /** Told after every completed command, with the running count. */
    private val onProgress: (Int) -> Unit = {},
) {
    /** Commands answered on this link (the status line's "N commands"). */
    private val count = AtomicInteger(0)

    /** Queue depth one, by design: the desktop's exchange() is blocking, so a
     *  second frame arriving mid-command means the two sides disagree about
     *  the protocol. Say so rather than quietly serialising it. */
    private val busy = AtomicBoolean(false)

    val commands: Int get() = count.get()

    /** Announce the dongle behind this phone. Ignored by the server today. */
    fun hello(device: String) {
        socket.send(JSONObject().put("type", "hello").put("device", device).toString())
    }

    fun ping() {
        socket.send(JSONObject().put("type", "ping").toString())
    }

    /** One text frame off the socket. Returns immediately; the dongle
     *  round-trip runs on [worker]. Safe to call from the reader thread. */
    fun onText(text: String) {
        val msg = try {
            JSONObject(text)
        } catch (_: Exception) {
            sendError("The desktop sent something that isn't JSON.")
            return
        }
        when (msg.optString("type")) {
            "data" -> onData(msg.optString("b64"))
            // The server never sends these, but a future one might; an
            // unknown type is not an error worth breaking the link over.
            "ping", "pong", "hello" -> Unit
            "error" -> Unit
            else -> Unit
        }
    }

    private fun onData(b64: String) {
        val command = try {
            Base64Wire.decode(b64)
        } catch (_: IllegalArgumentException) {
            sendError("The desktop sent a data frame that isn't valid base64.")
            return
        }
        if (!busy.compareAndSet(false, true)) {
            sendError(
                "A command is already in flight — the relay carries one at a time."
            )
            return
        }
        worker.execute {
            try {
                // Send the answer even when it's empty: the desktop is
                // blocked on its own wait, and an empty frame lets it time
                // out on the honest bytes rather than hang on nothing.
                val reply = try {
                    transport.exchange(command, exchangeTimeoutMs)
                } catch (e: Exception) {
                    sendError(e.message ?: "The dongle didn't answer.")
                    return@execute
                }
                socket.send(
                    JSONObject()
                        .put("type", "data")
                        .put("b64", Base64Wire.encode(reply))
                        .toString()
                )
                onProgress(count.incrementAndGet())
            } finally {
                busy.set(false)
            }
        }
    }

    private fun sendError(detail: String) {
        // An error frame is a report, not a hang-up: the dongle can refuse one
        // command (NO DATA, a dropped BLE packet) and answer the next.
        socket.send(JSONObject().put("type", "error").put("detail", detail).toString())
    }
}

/** The socket, seen from the relay: one text frame out. */
interface RelaySocket {
    /** True if the frame was queued. A false is the link already gone. */
    fun send(text: String): Boolean
}

/**
 * Base64, by hand.
 *
 * android.util.Base64 would drag the framework into this file, and
 * java.util.Base64 is API 26 while this app still ships to API 21 (the Autel
 * tablet). It is thirty lines; the alternative is a dependency or a floor.
 */
object Base64Wire {
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    private val REVERSE = IntArray(128) { -1 }.also {
        for (i in ALPHABET.indices) it[ALPHABET[i].code] = i
    }

    fun encode(data: ByteArray): String {
        val out = StringBuilder((data.size + 2) / 3 * 4)
        var i = 0
        while (i + 2 < data.size) {
            val n = (data[i].toInt() and 0xFF shl 16) or
                (data[i + 1].toInt() and 0xFF shl 8) or
                (data[i + 2].toInt() and 0xFF)
            out.append(ALPHABET[n ushr 18 and 0x3F])
            out.append(ALPHABET[n ushr 12 and 0x3F])
            out.append(ALPHABET[n ushr 6 and 0x3F])
            out.append(ALPHABET[n and 0x3F])
            i += 3
        }
        when (data.size - i) {
            1 -> {
                val n = data[i].toInt() and 0xFF shl 16
                out.append(ALPHABET[n ushr 18 and 0x3F])
                out.append(ALPHABET[n ushr 12 and 0x3F])
                out.append("==")
            }
            2 -> {
                val n = (data[i].toInt() and 0xFF shl 16) or (data[i + 1].toInt() and 0xFF shl 8)
                out.append(ALPHABET[n ushr 18 and 0x3F])
                out.append(ALPHABET[n ushr 12 and 0x3F])
                out.append(ALPHABET[n ushr 6 and 0x3F])
                out.append('=')
            }
        }
        return out.toString()
    }

    /** @throws IllegalArgumentException on anything that isn't base64. */
    fun decode(text: String): ByteArray {
        val out = java.io.ByteArrayOutputStream(text.length * 3 / 4 + 3)
        var acc = 0
        var bits = 0
        for (c in text) {
            if (c == '=' || c == '\n' || c == '\r' || c == ' ') continue
            val v = if (c.code < 128) REVERSE[c.code] else -1
            require(v >= 0) { "not base64: '$c'" }
            acc = acc shl 6 or v
            bits += 6
            if (bits >= 8) {
                bits -= 8
                out.write(acc ushr bits and 0xFF)
            }
        }
        return out.toByteArray()
    }
}
