package com.carharness.app.harness

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLEncoder

data class DesktopServer(val name: String, val host: String, val port: Int)

/**
 * Client for the desktop car-harness: UDP discovery, pairing, and the
 * token-authenticated REST API. Pairing state persists in SharedPreferences.
 */
class HarnessClient(context: Context) {
    private val prefs = context.getSharedPreferences("harness", Context.MODE_PRIVATE)

    val paired: Boolean get() = token().isNotEmpty()
    fun serverName(): String = prefs.getString("serverName", "") ?: ""
    fun host(): String = prefs.getString("host", "") ?: ""
    fun port(): Int = prefs.getInt("port", 8765)
    private fun token(): String = prefs.getString("token", "") ?: ""

    fun unpair() {
        prefs.edit().clear().apply()
    }

    /**
     * The Relay-mode WebSocket URL for this paired desktop, token and all.
     *
     * Two ways home and the port tells them apart: on the LAN it's the plain
     * ws:// on 8765, and through a Cloudflare tunnel it's wss:// on 443 with no
     * port suffix at all. Empty when there's no desktop to relay to.
     */
    fun relayUrl(deviceName: String): String {
        val h = host()
        if (h.isEmpty()) return ""
        val p = port()
        val base = if (p == 443) "wss://$h" else "ws://$h:$p"
        return base + "/api/remote/ws?token=" + enc(token()) + "&name=" + enc(deviceName)
    }

    private fun enc(value: String): String =
        URLEncoder.encode(value, "UTF-8").replace("+", "%20")

    /** Broadcast CARHARNESS_DISCOVER and collect replies for ~2.5s. */
    fun discover(): List<DesktopServer> {
        val found = mutableMapOf<String, DesktopServer>()
        DatagramSocket().use { socket ->
            socket.broadcast = true
            socket.soTimeout = 500
            val payload = "CARHARNESS_DISCOVER".toByteArray(Charsets.US_ASCII)
            val addresses = listOf("255.255.255.255")
            for (addr in addresses) {
                try {
                    socket.send(
                        DatagramPacket(payload, payload.size, InetAddress.getByName(addr), 8766)
                    )
                } catch (_: Exception) {
                }
            }
            val deadline = System.currentTimeMillis() + 2500
            val buf = ByteArray(512)
            while (System.currentTimeMillis() < deadline) {
                val packet = DatagramPacket(buf, buf.size)
                try {
                    socket.receive(packet)
                } catch (_: SocketTimeoutException) {
                    continue
                }
                try {
                    val json = JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
                    if (json.optString("app") == "car-harness") {
                        val hostIp = packet.address.hostAddress ?: continue
                        found[hostIp] = DesktopServer(
                            name = json.optString("name", hostIp),
                            host = hostIp,
                            port = json.optInt("port", 8765),
                        )
                    }
                } catch (_: Exception) {
                }
            }
        }
        return found.values.toList()
    }

    /** Claim a pairing code shown on the desktop; stores the token on success. */
    fun pair(host: String, port: Int, code: String, deviceName: String) {
        val body = JSONObject().put("code", code).put("deviceName", deviceName)
        val response = http("POST", host, port, "/api/pair/claim", body.toString(), auth = false)
        val json = JSONObject(response)
        val newToken = json.optString("token")
        if (newToken.isEmpty()) throw IOException("Pairing failed: no token returned.")
        prefs.edit()
            .putString("token", newToken)
            .putString("host", host)
            .putInt("port", port)
            .putString("serverName", json.optString("serverName", host))
            .apply()
    }

    fun get(path: String): JSONObject = JSONObject(http("GET", host(), port(), path, null))
    fun post(path: String, body: JSONObject): JSONObject =
        JSONObject(http("POST", host(), port(), path, body.toString()))

    fun adapters(): JSONArray = get("/api/adapters").optJSONArray("adapters") ?: JSONArray()
    fun modules(): JSONArray = get("/api/modules").optJSONArray("modules") ?: JSONArray()
    fun profiles(): JSONArray = get("/api/profiles").optJSONArray("profiles") ?: JSONArray()

    fun obdScan(adapterId: String?, sim: Boolean): JSONObject {
        var path = "/api/obd/scan?mode=" + if (sim) "sim" else "real"
        if (!sim && adapterId != null) path += "&adapter=$adapterId"
        return get(path)
    }

    fun obdClear(adapterId: String?, sim: Boolean): JSONObject =
        post(
            "/api/obd/clear",
            JSONObject()
                .put("adapter", adapterId ?: JSONObject.NULL)
                .put("mode", if (sim) "sim" else "real")
                .put("confirm", true)
        )

    fun status(profile: String, sim: Boolean, adapterId: String?): JSONObject {
        var path = "/api/status?profile=$profile&mode=" + if (sim) "sim" else "real"
        if (adapterId != null) path += "&adapter=$adapterId"
        return get(path)
    }

    private fun http(
        method: String,
        host: String,
        port: Int,
        path: String,
        body: String?,
        auth: Boolean = true,
    ): String {
        if (host.isEmpty()) throw IOException("Not paired with a desktop yet.")
        val conn = URL("http://$host:$port$path").openConnection() as HttpURLConnection
        conn.requestMethod = method
        conn.connectTimeout = 5000
        conn.readTimeout = 60000
        if (auth) conn.setRequestProperty("Authorization", "Bearer ${token()}")
        if (body != null) {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        }
        try {
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            if (code == 401) throw IOException("Not paired any more — the desktop revoked this phone.")
            if (code == 403) throw IOException(parseDetail(text, "Wrong or expired pairing code."))
            if (code !in 200..299) throw IOException(parseDetail(text, "Desktop returned HTTP $code."))
            return text
        } finally {
            conn.disconnect()
        }
    }

    private fun parseDetail(text: String, fallback: String): String = try {
        JSONObject(text).optString("detail").ifEmpty { fallback }
    } catch (_: Exception) {
        fallback
    }
}
