package com.carharness.app.obd

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import java.io.IOException
import java.util.UUID

/**
 * Classic-Bluetooth ELM327 (SPP/RFCOMM). The device must be paired in Android
 * settings first (PIN usually 1234 or 0000).
 */
@SuppressLint("MissingPermission") // caller checks BLUETOOTH_CONNECT before connecting
class BluetoothSppTransport(private val device: BluetoothDevice) : ObdTransport {
    override val label = "Bluetooth ${device.name ?: device.address}"

    private var socket: android.bluetooth.BluetoothSocket? = null

    companion object {
        private val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    }

    override fun open() {
        val s = try {
            device.createRfcommSocketToServiceRecord(SPP_UUID)
        } catch (e: Exception) {
            throw IOException("Could not create a Bluetooth link: ${e.message}")
        }
        try {
            s.connect()
        } catch (e: Exception) {
            try { s.close() } catch (_: Exception) {}
            throw IOException(
                "Could not connect to ${device.name ?: device.address}. Is the dongle " +
                    "powered (plugged into the car) and paired in Android Bluetooth settings?"
            )
        }
        socket = s
    }

    override fun exchange(data: ByteArray, timeoutMs: Long): ByteArray {
        val s = socket ?: throw IOException("Bluetooth link is not open.")
        val input = s.inputStream
        // drain anything stale
        while (input.available() > 0) input.read()
        s.outputStream.write(data)
        s.outputStream.flush()
        val out = java.io.ByteArrayOutputStream()
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (input.available() > 0) {
                val b = input.read()
                if (b < 0) break
                out.write(b)
                if (b == '>'.code) break
            } else {
                Thread.sleep(20)
            }
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
