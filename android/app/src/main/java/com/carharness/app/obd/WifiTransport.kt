package com.carharness.app.obd

import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket

/** ELM327 over WiFi (the dongle's own AP, usually 192.168.0.10:35000). */
class WifiTransport(private val host: String, private val port: Int) : ObdTransport {
    override val label = "WiFi $host:$port"
    private var socket: Socket? = null

    override fun open() {
        val s = Socket()
        try {
            s.connect(InetSocketAddress(host, port), 5000)
        } catch (e: Exception) {
            throw IOException(
                "Could not reach $host:$port — is the phone joined to the dongle's " +
                    "WiFi network (usually 'WiFi_OBDII')?"
            )
        }
        socket = s
    }

    override fun exchange(data: ByteArray, timeoutMs: Long): ByteArray {
        val s = socket ?: throw IOException("WiFi link is not open.")
        s.getOutputStream().write(data)
        s.getOutputStream().flush()
        val buf = ByteArray(512)
        val out = java.io.ByteArrayOutputStream()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            s.soTimeout = 300
            val n = try {
                s.getInputStream().read(buf)
            } catch (e: java.net.SocketTimeoutException) {
                continue
            }
            if (n < 0) throw IOException("$host:$port closed the connection.")
            out.write(buf, 0, n)
            if (buf.sliceArray(0 until n).contains('>'.code.toByte())) break
        }
        return out.toByteArray()
    }

    override fun close() {
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
    }
}
