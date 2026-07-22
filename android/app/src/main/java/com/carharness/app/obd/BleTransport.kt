package com.carharness.app.obd

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.content.Context
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Bluetooth LE ELM327 (Veepeak OBDCheck BLE and clones), via GATT.
 *
 * The rule that makes these dongles work (verified against a real VEEPEAK):
 * the write and notify characteristics MUST come from the same vendor UART
 * service — FFF0 on the Veepeak (notify FFF1, write FFF2). Grabbing the first
 * notify characteristic on the device gets the standard "Service Changed"
 * channel and the ELM's replies are never seen.
 */
@SuppressLint("MissingPermission") // caller checks BLUETOOTH_CONNECT/SCAN first
class BleTransport(private val context: Context, private val device: BluetoothDevice) :
    ObdTransport {

    override val label = "BLE ${device.name ?: device.address}"

    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null
    private var notifyChar: BluetoothGattCharacteristic? = null

    private val buffer = ByteArrayOutputStream()
    private val bufferLock = Object()
    @Volatile private var promptLatch = CountDownLatch(1)
    @Volatile private var connectLatch = CountDownLatch(1)
    @Volatile private var servicesLatch = CountDownLatch(1)
    @Volatile private var descriptorLatch = CountDownLatch(1)
    @Volatile private var connectionState = BluetoothProfile.STATE_DISCONNECTED

    companion object {
        private val CCC_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        private val SKIP_SERVICES = setOf("1800", "1801", "180a", "180f")
        private val PREFERRED = listOf(
            "fff0", "49535343-fe7d-4ae5-8fa9-9fafd205e455", "ffe0", "6e400001", "ffb0", "18f0"
        )

        private fun short(uuid: UUID): String {
            val u = uuid.toString().lowercase()
            return if (u.endsWith("-0000-1000-8000-00805f9b34fb")) u.substring(4, 8) else u
        }
    }

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            connectionState = newState
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connectLatch.countDown()
                g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                connectLatch.countDown()
                servicesLatch.countDown()
                promptLatch.countDown()
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            servicesLatch.countDown()
        }

        override fun onDescriptorWrite(
            g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int
        ) {
            descriptorLatch.countDown()
        }

        // Android 12 and below deliver notifications here ...
        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic
        ) {
            @Suppress("DEPRECATION")
            onData(characteristic.value ?: return)
        }

        // ... Android 13+ delivers them here.
        override fun onCharacteristicChanged(
            g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray
        ) {
            onData(value)
        }

        private fun onData(value: ByteArray) {
            synchronized(bufferLock) {
                buffer.write(value)
            }
            if (value.contains('>'.code.toByte())) promptLatch.countDown()
        }
    }

    override fun open() {
        connectLatch = CountDownLatch(1)
        servicesLatch = CountDownLatch(1)
        val g = device.connectGatt(context, false, callback)
            ?: throw IOException("Could not start a BLE connection.")
        gatt = g
        if (!connectLatch.await(15, TimeUnit.SECONDS) ||
            connectionState != BluetoothProfile.STATE_CONNECTED
        ) {
            close()
            throw IOException(
                "Could not connect to ${device.name ?: device.address}. Is the dongle " +
                    "plugged into the car and in range? Reseating it wakes it up."
            )
        }
        if (!servicesLatch.await(10, TimeUnit.SECONDS)) {
            close()
            throw IOException("Connected but the dongle's services never appeared.")
        }

        // Pick the UART pipe: write + notify from the SAME vendor service.
        data class Pair2(val service: String, val w: BluetoothGattCharacteristic, val n: BluetoothGattCharacteristic)
        val pairs = mutableListOf<Pair2>()
        for (service in g.services) {
            val s = short(service.uuid)
            if (s in SKIP_SERVICES) continue
            var w: BluetoothGattCharacteristic? = null
            var n: BluetoothGattCharacteristic? = null
            for (ch in service.characteristics) {
                val p = ch.properties
                val canNotify =
                    p and (BluetoothGattCharacteristic.PROPERTY_NOTIFY or
                        BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0
                val canWrite =
                    p and (BluetoothGattCharacteristic.PROPERTY_WRITE or
                        BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0
                if (n == null && canNotify) n = ch
                if (w == null && canWrite) w = ch
            }
            if (w != null && n != null) pairs.add(Pair2(s, w, n))
        }
        pairs.sortBy { PREFERRED.indexOf(it.service).let { i -> if (i == -1) 99 else i } }
        val pair = pairs.firstOrNull()
            ?: run {
                close()
                throw IOException("No UART-style service found on this dongle.")
            }
        writeChar = pair.w
        notifyChar = pair.n

        // Enable notifications (client side + the CCC descriptor on the dongle).
        g.setCharacteristicNotification(pair.n, true)
        val ccc = pair.n.getDescriptor(CCC_UUID)
        if (ccc != null) {
            descriptorLatch = CountDownLatch(1)
            val useIndicate =
                pair.n.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY == 0
            @Suppress("DEPRECATION")
            ccc.value = if (useIndicate) {
                BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
            } else {
                BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            }
            @Suppress("DEPRECATION")
            g.writeDescriptor(ccc)
            descriptorLatch.await(5, TimeUnit.SECONDS)
        }

        // Wake: a bare CR clears any half-typed command in the dongle.
        try {
            rawWrite("\r".toByteArray())
            Thread.sleep(300)
        } catch (_: Exception) {
        }
        synchronized(bufferLock) { buffer.reset() }
    }

    private fun rawWrite(data: ByteArray) {
        val g = gatt ?: throw IOException("BLE link is not open.")
        val w = writeChar ?: throw IOException("BLE link is not open.")
        val noResponse =
            w.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0
        @Suppress("DEPRECATION")
        run {
            w.writeType = if (noResponse) {
                BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            } else {
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            }
            w.value = data
            if (!g.writeCharacteristic(w)) throw IOException("BLE write failed.")
        }
    }

    override fun exchange(data: ByteArray, timeoutMs: Long): ByteArray {
        if (connectionState != BluetoothProfile.STATE_CONNECTED) {
            throw IOException("BLE link dropped.")
        }
        synchronized(bufferLock) { buffer.reset() }
        promptLatch = CountDownLatch(1)
        rawWrite(data)
        promptLatch.await(timeoutMs, TimeUnit.MILLISECONDS)
        synchronized(bufferLock) {
            return buffer.toByteArray()
        }
    }

    override fun close() {
        try {
            gatt?.disconnect()
        } catch (_: Exception) {
        }
        try {
            gatt?.close()
        } catch (_: Exception) {
        }
        gatt = null
        writeChar = null
        notifyChar = null
    }
}
