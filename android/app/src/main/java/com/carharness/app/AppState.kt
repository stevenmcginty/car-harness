package com.carharness.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult as BleScanResult
import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.setValue
import com.carharness.app.harness.DesktopServer
import com.carharness.app.harness.HarnessClient
import com.carharness.app.obd.BleTransport
import com.carharness.app.obd.BluetoothSppTransport
import com.carharness.app.obd.DemoTransport
import com.carharness.app.obd.EnhancedReading
import com.carharness.app.obd.Dtc
import com.carharness.app.obd.DtcRecord
import com.carharness.app.obd.Elm327Session
import com.carharness.app.obd.LiveReading
import com.carharness.app.obd.ObdController
import com.carharness.app.obd.ObdTransport
import com.carharness.app.obd.ScanResult
import com.carharness.app.obd.UdsScanReport
import com.carharness.app.obd.WifiTransport
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

sealed class ConnState {
    data object Idle : ConnState()
    data object Connecting : ConnState()
    data class Connected(val label: String, val firmware: String, val voltage: String) : ConnState()
    data class Error(val message: String) : ConnState()
}

data class AdapterRow(val id: String, val name: String, val typeId: String)
data class ModuleRow(val id: String, val name: String, val profile: String, val builtin: Boolean)

@SuppressLint("MissingPermission") // every BT/BLE entry point is permission-gated in MainActivity
class AppState(private val context: Context, private val scope: CoroutineScope) {

    val harness = HarnessClient(context)

    init {
        Dtc.load(context)
    }

    // ---------------- car connection ----------------
    var transportChoice by mutableStateOf("demo") // demo | ble | bt | wifi
    var connState by mutableStateOf<ConnState>(ConnState.Idle)
    private var session: Elm327Session? = null
    var controller: ObdController? = null
        private set

    var wifiHost by mutableStateOf("192.168.0.10")
    var wifiPort by mutableStateOf("35000")

    val bleDevices = mutableStateListOf<Pair<String, String>>() // name to address
    var bleScanning by mutableStateOf(false)
    val bondedDevices = mutableStateListOf<Pair<String, String>>()

    private var bleScanCallback: ScanCallback? = null

    private fun bluetoothAdapter(): BluetoothAdapter? =
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    fun startBleScan() {
        if (bleScanning) return
        val adapter = bluetoothAdapter() ?: return
        val scanner = adapter.bluetoothLeScanner ?: return
        bleDevices.clear()
        bleScanning = true
        val cb = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: BleScanResult) {
                val name = result.device.name ?: return // unnamed devices are never the dongle
                val entry = name to result.device.address
                if (bleDevices.none { it.second == entry.second }) bleDevices.add(entry)
            }
        }
        bleScanCallback = cb
        scanner.startScan(cb)
        scope.launch {
            delay(12000)
            stopBleScan()
        }
    }

    fun stopBleScan() {
        val adapter = bluetoothAdapter()
        bleScanCallback?.let { cb ->
            try {
                adapter?.bluetoothLeScanner?.stopScan(cb)
            } catch (_: Exception) {
            }
        }
        bleScanCallback = null
        bleScanning = false
    }

    fun refreshBonded() {
        bondedDevices.clear()
        bluetoothAdapter()?.bondedDevices?.forEach { d ->
            bondedDevices.add((d.name ?: d.address) to d.address)
        }
    }

    fun connect(kind: String, address: String? = null) {
        if (connState is ConnState.Connecting) return
        stopBleScan()
        disconnect()
        connState = ConnState.Connecting
        scope.launch(Dispatchers.IO) {
            try {
                val transport: ObdTransport = when (kind) {
                    "demo" -> DemoTransport()
                    "wifi" -> WifiTransport(wifiHost.trim(), wifiPort.trim().toIntOrNull() ?: 35000)
                    "bt" -> {
                        val device = remoteDevice(address!!)
                        BluetoothSppTransport(device)
                    }
                    "ble" -> {
                        val device = remoteDevice(address!!)
                        BleTransport(context, device)
                    }
                    else -> throw IllegalArgumentException("unknown transport")
                }
                val s = Elm327Session(transport)
                s.open()
                val voltage = s.voltage()
                session = s
                controller = ObdController(s, simulated = kind == "demo")
                withContext(Dispatchers.Main) {
                    connState = ConnState.Connected(transport.label, s.firmware, voltage)
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    connState = ConnState.Error(e.message ?: "Could not connect.")
                }
            }
        }
    }

    private fun remoteDevice(address: String): BluetoothDevice =
        bluetoothAdapter()?.getRemoteDevice(address)
            ?: throw IllegalStateException("Bluetooth is off.")

    fun disconnect() {
        liveOn = false
        liveJob?.cancel()
        liveJob = null
        controller = null
        val s = session
        session = null
        if (s != null) {
            scope.launch(Dispatchers.IO) { s.close() }
        }
        connState = ConnState.Idle
        readings = emptyList()
    }

    // ---------------- live data ----------------
    var liveOn by mutableStateOf(false)
    var readings by mutableStateOf<List<LiveReading>>(emptyList())
    var liveError by mutableStateOf("")
    private var liveJob: Job? = null

    fun setLive(on: Boolean) {
        liveOn = on
        liveJob?.cancel()
        liveJob = null
        liveError = ""
        if (!on) return
        val ctrl = controller ?: run {
            liveOn = false
            liveError = "Connect to the car first."
            return
        }
        liveJob = scope.launch(Dispatchers.IO) {
            while (liveOn) {
                try {
                    val r = ctrl.live()
                    withContext(Dispatchers.Main) { readings = r }
                } catch (e: Exception) {
                    withContext(Dispatchers.Main) {
                        liveError = e.message ?: "Live data failed."
                        liveOn = false
                    }
                    break
                }
                delay(400)
            }
        }
    }

    // ---------------- fault codes ----------------
    var scanning by mutableStateOf(false)
    var clearing by mutableStateOf(false)
    var scanResult by mutableStateOf<ScanResult?>(null)
    var faultError by mutableStateOf("")

    fun runScan() {
        val ctrl = controller ?: run {
            faultError = "Connect to the car first (Car tab)."
            return
        }
        if (scanning) return
        scanning = true
        faultError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val result = ctrl.scan()
                withContext(Dispatchers.Main) { scanResult = result }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    faultError = e.message ?: "Scan failed."
                }
            } finally {
                withContext(Dispatchers.Main) { scanning = false }
            }
        }
    }

    fun runClear() {
        val ctrl = controller ?: return
        if (clearing) return
        clearing = true
        faultError = ""
        scope.launch(Dispatchers.IO) {
            try {
                ctrl.clear()
                val result = ctrl.scan()
                withContext(Dispatchers.Main) { scanResult = result }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    faultError = e.message ?: "Clear failed."
                }
            } finally {
                withContext(Dispatchers.Main) { clearing = false }
            }
        }
    }

    // ------------- manufacturer module scan (BMW / Mazda / Peugeot) ---------
    var moduleBrand by mutableStateOf("bmw") // bmw | mazda | peugeot
    var modScanning by mutableStateOf(false)
    var modProgress by mutableStateOf("")
    var modReport by mutableStateOf<UdsScanReport?>(null)
    var modError by mutableStateOf("")
    var modClearingAddr by mutableStateOf<Int?>(null)

    // Mazda enhanced (mode-22) values / broadcast snapshot, read separately
    // from the module scan. One card shows whichever ran last.
    var mazdaExtras by mutableStateOf<List<EnhancedReading>>(emptyList())
    var mazdaExtrasBusy by mutableStateOf(false)
    var mazdaExtrasRead by mutableStateOf(false) // true once a read completed
    var mazdaExtrasSource by mutableStateOf("")  // "extras" | "snapshot"

    fun chooseModuleBrand(brand: String) {
        if (brand == moduleBrand) return
        moduleBrand = brand
        modError = ""
    }

    fun runModuleScan() {
        val ctrl = controller ?: run {
            modError = "Connect to the car first (Car tab)."
            return
        }
        if (modScanning) return
        setLive(false) // a module scan retargets the ELM; pause the dashboard
        val brand = moduleBrand
        modScanning = true
        modError = ""
        modProgress = "Starting module scan…"
        scope.launch(Dispatchers.IO) {
            try {
                val onStep: (String) -> Unit = { step ->
                    scope.launch(Dispatchers.Main) { modProgress = step }
                }
                val report = when (brand) {
                    "mazda" -> ctrl.mazdaScan(onStep)
                    "peugeot" -> ctrl.peugeotScan(onStep)
                    else -> ctrl.bmwScan(onStep)
                }
                withContext(Dispatchers.Main) {
                    modReport = report
                    if (!report.ok) modError = report.message
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    modError = e.message ?: "Module scan failed."
                }
            } finally {
                withContext(Dispatchers.Main) {
                    modScanning = false
                    modProgress = ""
                }
            }
        }
    }

    fun runModuleClear(address: Int) {
        val ctrl = controller ?: return
        val report = modReport ?: return
        if (modClearingAddr != null || modScanning) return
        modClearingAddr = address
        modError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val fresh = when (report.brand) {
                    "mazda" -> ctrl.mazdaClear(address)
                    "peugeot" -> ctrl.peugeotClear(address)
                    else -> ctrl.bmwClear(address)
                }
                withContext(Dispatchers.Main) {
                    modReport = modReport?.let { r ->
                        r.copy(modules = r.modules.map { m ->
                            if (m.address == address) m.copy(dtcs = fresh, error = null) else m
                        })
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    modError = e.message ?: "Clear failed."
                }
            } finally {
                withContext(Dispatchers.Main) { modClearingAddr = null }
            }
        }
    }

    private fun runMazdaReadings(
        source: String,
        read: suspend (ObdController, (String) -> Unit) -> List<EnhancedReading>,
    ) {
        val ctrl = controller ?: run {
            modError = "Connect to the car first (Car tab)."
            return
        }
        if (mazdaExtrasBusy || modScanning) return
        setLive(false) // retargets the ELM; pause the dashboard
        mazdaExtrasBusy = true
        modError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val extras = read(ctrl) { step ->
                    scope.launch(Dispatchers.Main) { modProgress = step }
                }
                withContext(Dispatchers.Main) {
                    mazdaExtras = extras
                    mazdaExtrasRead = true
                    mazdaExtrasSource = source
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    modError = e.message ?: "Read failed."
                }
            } finally {
                withContext(Dispatchers.Main) {
                    mazdaExtrasBusy = false
                    modProgress = ""
                }
            }
        }
    }

    /** Mazda mode-22 values asked from each module (PCM, TCM, cluster, body). */
    fun runMazdaExtras() = runMazdaReadings("extras") { ctrl, onStep ->
        ctrl.mazdaEnhanced(onStep)
    }

    /** Passive listen to the car's own broadcast frames — sends nothing. */
    fun runMazdaSnapshot() = runMazdaReadings("snapshot") { ctrl, onStep ->
        ctrl.mazdaSnapshot(onStep)
    }

    // ---------------- desktop pairing + remote ----------------
    var discovering by mutableStateOf(false)
    val servers = mutableStateListOf<DesktopServer>()
    var manualHost by mutableStateOf("")
    var pairCode by mutableStateOf("")
    var pairBusy by mutableStateOf(false)
    var desktopError by mutableStateOf("")
    var desktopPaired by mutableStateOf(false)

    val remoteAdapters = mutableStateListOf<AdapterRow>()
    val remoteModules = mutableStateListOf<ModuleRow>()
    var remoteAdapterId by mutableStateOf<String?>(null)
    var remoteSim by mutableStateOf(false)
    var remoteBusy by mutableStateOf(false)
    var remoteScan by mutableStateOf<ScanResult?>(null)
    var remoteMessage by mutableStateOf("")

    init {
        desktopPaired = harness.paired
    }

    fun discoverDesktops() {
        if (discovering) return
        discovering = true
        desktopError = ""
        servers.clear()
        scope.launch(Dispatchers.IO) {
            val found = try {
                harness.discover()
            } catch (e: Exception) {
                emptyList()
            }
            withContext(Dispatchers.Main) {
                servers.addAll(found)
                discovering = false
                if (found.isEmpty()) {
                    desktopError =
                        "No desktop found on this WiFi. Make sure Car Harness is running " +
                            "on the PC, both are on the same network, and Windows Firewall " +
                            "allowed Python. Or type the PC's IP below."
                }
            }
        }
    }

    fun pairWith(host: String, port: Int) {
        if (pairBusy) return
        val code = pairCode.trim()
        if (code.length != 6) {
            desktopError = "Enter the 6-digit code shown on the desktop (Pair phone…)."
            return
        }
        pairBusy = true
        desktopError = ""
        scope.launch(Dispatchers.IO) {
            try {
                harness.pair(host, port, code, android.os.Build.MODEL ?: "Phone")
                withContext(Dispatchers.Main) {
                    desktopPaired = true
                    pairCode = ""
                    refreshDesktop()
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    desktopError = e.message ?: "Pairing failed."
                }
            } finally {
                withContext(Dispatchers.Main) { pairBusy = false }
            }
        }
    }

    fun unpair() {
        harness.unpair()
        desktopPaired = false
        remoteAdapters.clear()
        remoteModules.clear()
        remoteScan = null
        remoteMessage = ""
    }

    fun refreshDesktop() {
        if (!harness.paired) return
        scope.launch(Dispatchers.IO) {
            try {
                val adapters = harness.adapters()
                val modules = harness.modules()
                withContext(Dispatchers.Main) {
                    remoteAdapters.clear()
                    for (i in 0 until adapters.length()) {
                        val a = adapters.getJSONObject(i)
                        remoteAdapters.add(
                            AdapterRow(a.getString("id"), a.optString("name"), a.optString("typeId"))
                        )
                    }
                    if (remoteAdapterId == null && remoteAdapters.isNotEmpty()) {
                        remoteAdapterId = remoteAdapters.first().id
                    }
                    remoteModules.clear()
                    for (i in 0 until modules.length()) {
                        val m = modules.getJSONObject(i)
                        remoteModules.add(
                            ModuleRow(
                                m.getString("id"), m.optString("name"),
                                m.optString("profile"), m.optBoolean("builtin")
                            )
                        )
                    }
                    desktopError = ""
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    desktopError = e.message ?: "Couldn't reach the desktop."
                }
            }
        }
    }

    fun remoteObdScan() {
        if (remoteBusy) return
        remoteBusy = true
        remoteMessage = ""
        scope.launch(Dispatchers.IO) {
            try {
                val json = harness.obdScan(remoteAdapterId, remoteSim)
                val result = parseRemoteScan(json)
                withContext(Dispatchers.Main) {
                    remoteScan = result
                    remoteMessage = if (result.ok) "" else result.message
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    remoteMessage = e.message ?: "Desktop scan failed."
                }
            } finally {
                withContext(Dispatchers.Main) { remoteBusy = false }
            }
        }
    }

    fun remoteObdClear() {
        if (remoteBusy) return
        remoteBusy = true
        scope.launch(Dispatchers.IO) {
            try {
                val json = harness.obdClear(remoteAdapterId, remoteSim)
                withContext(Dispatchers.Main) {
                    remoteMessage = json.optString("message")
                }
                if (json.optBoolean("ok")) remoteObdScanBlockingRefresh()
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    remoteMessage = e.message ?: "Desktop clear failed."
                }
            } finally {
                withContext(Dispatchers.Main) { remoteBusy = false }
            }
        }
    }

    private suspend fun remoteObdScanBlockingRefresh() {
        try {
            val json = harness.obdScan(remoteAdapterId, remoteSim)
            val result = parseRemoteScan(json)
            withContext(Dispatchers.Main) { remoteScan = result }
        } catch (_: Exception) {
        }
    }

    private fun parseRemoteScan(json: JSONObject): ScanResult {
        fun codes(name: String, status: String): List<DtcRecord> {
            val arr = json.optJSONArray(name) ?: return emptyList()
            return (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                DtcRecord(o.optString("code"), o.optString("description"), status)
            }
        }
        return ScanResult(
            ok = json.optBoolean("ok"),
            simulated = json.optBoolean("simulated"),
            vin = json.optString("vin").ifEmpty { null },
            protocol = json.optString("protocol"),
            voltage = json.optString("voltage"),
            firmware = json.optString("firmware"),
            milOn = json.optBoolean("milOn"),
            dtcCount = json.optInt("dtcCount"),
            stored = codes("stored", "stored"),
            pending = codes("pending", "pending"),
            permanent = codes("permanent", "permanent"),
            message = json.optString("message"),
        )
    }
}
