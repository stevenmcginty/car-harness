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
import com.carharness.app.obd.BrandAction
import com.carharness.app.obd.Brands
import com.carharness.app.obd.DemoTransport
import com.carharness.app.obd.EnhancedReading
import com.carharness.app.obd.Dtc
import com.carharness.app.obd.DtcRecord
import com.carharness.app.obd.Elm327Session
import com.carharness.app.obd.LiveReading
import com.carharness.app.obd.ObdController
import com.carharness.app.obd.ObdTransport
import com.carharness.app.obd.ResetOutcome
import com.carharness.app.obd.ServiceReset
import com.carharness.app.obd.ServiceResets
import com.carharness.app.obd.ScanResult
import com.carharness.app.obd.UdsScanReport
import com.carharness.app.obd.VehicleIdentity
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

    // What the car is, read once at connect (Mode 09 VIN -> make + likely year).
    // Cleared on disconnect. vin == null once identifying is false means the car
    // didn't answer (ignition off?) — the Car tab still shows the card.
    var vehicle by mutableStateOf<VehicleIdentity?>(null)
        private set
    var identifying by mutableStateOf(false)
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
            var transport: ObdTransport? = null
            try {
                val t: ObdTransport = when (kind) {
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
                transport = t
                val s = Elm327Session(t)
                s.open()
                val voltage = s.voltage()
                session = s
                controller = ObdController(s, simulated = kind == "demo")
                if (t is BleTransport) {
                    // Surface a mid-session BLE drop instead of showing
                    // "Connected" forever with every button silently failing.
                    t.onLinkLost = {
                        scope.launch(Dispatchers.Main) {
                            if (session === s) {
                                disconnect()
                                connState = ConnState.Error(
                                    "BLE link lost — the dongle went out of range or powered " +
                                        "off. Reconnect from the Car tab."
                                )
                            }
                        }
                    }
                }
                withContext(Dispatchers.Main) {
                    connState = ConnState.Connected(t.label, s.firmware, voltage)
                    identifying = true
                }
                // Identify the car straight away — one Mode 09 VIN read + decode,
                // before any live polling starts — so the Car tab headlines the
                // VIN and what the car is the moment we're connected.
                val ident = try {
                    controller?.identify()
                } catch (_: Exception) {
                    null
                }
                withContext(Dispatchers.Main) {
                    if (session === s) {
                        vehicle = ident
                        identifying = false
                    }
                }
            } catch (e: Exception) {
                // Elm327Session.open() closes the transport when the init fails,
                // but a failure before/around it (voltage, wiring) must not leak
                // a live GATT link or socket. Closing twice is harmless.
                try {
                    transport?.close()
                } catch (_: Exception) {
                }
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
        vehicle = null
        identifying = false
        readings = emptyList()
        // Results and errors from the old connection are another car's story --
        // showing them as if still live would mislead on the next vehicle.
        scanResult = null
        faultError = ""
        liveError = ""
        modReport = null
        modError = ""
        modProgress = ""
        extras = emptyList()
        extrasRead = false
        extrasBrand = ""
        resetMessage = ""
        resetProgress = ""
        resetLog = emptyList()
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
        setLive(false) // don't make the scan fight the dashboard for the ELM
        scanning = true
        faultError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val result = ctrl.scan()
                withContext(Dispatchers.Main) { scanResult = result }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    // After an intentional disconnect the failure is expected --
                    // don't surface it as if the scan itself went wrong.
                    if (controller === ctrl) faultError = e.message ?: "Scan failed."
                }
            } finally {
                withContext(Dispatchers.Main) { scanning = false }
            }
        }
    }

    fun runClear() {
        val ctrl = controller ?: return
        if (clearing) return
        setLive(false) // don't make the clear fight the dashboard for the ELM
        clearing = true
        faultError = ""
        scope.launch(Dispatchers.IO) {
            try {
                ctrl.clear()
                val result = ctrl.scan()
                withContext(Dispatchers.Main) { scanResult = result }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (controller === ctrl) faultError = e.message ?: "Clear failed."
                }
            } finally {
                withContext(Dispatchers.Main) { clearing = false }
            }
        }
    }

    // ------------- manufacturer module scan (see obd/Brands.kt) -------------
    var moduleBrand by mutableStateOf(Brands.all.first().id)
    var modScanning by mutableStateOf(false)
    var modProgress by mutableStateOf("")
    var modReport by mutableStateOf<UdsScanReport?>(null)
    var modError by mutableStateOf("")
    var modClearingAddr by mutableStateOf<Int?>(null)

    // Brand-specific enhanced values (Mazda mode-22 / snapshot, Peugeot health
    // check), read separately from the module scan. One card shows whichever
    // ran last; extrasBrand keeps a Mazda card from showing under Peugeot.
    var extras by mutableStateOf<List<EnhancedReading>>(emptyList())
    var extrasBusy by mutableStateOf(false)
    var extrasRead by mutableStateOf(false)     // true once a read completed
    var extrasSource by mutableStateOf("")      // BrandAction.id that produced it
    var extrasBrand by mutableStateOf("")       // brand that produced the card
    var extrasRunningId by mutableStateOf<String?>(null) // BrandAction.id in flight

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
        val brand = Brands.byId(moduleBrand) ?: run {
            modError = "Unknown brand '$moduleBrand'."
            return
        }
        if (modScanning) return
        setLive(false) // a module scan retargets the ELM; pause the dashboard
        modScanning = true
        modError = ""
        modProgress = "Starting module scan…"
        scope.launch(Dispatchers.IO) {
            try {
                val onStep: (String) -> Unit = { step ->
                    scope.launch(Dispatchers.Main) { modProgress = step }
                }
                val report = brand.scan(ctrl, onStep)
                withContext(Dispatchers.Main) {
                    modReport = report
                    if (!report.ok) modError = report.message
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (controller === ctrl) modError = e.message ?: "Module scan failed."
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
        val brand = Brands.byId(report.brand) ?: return
        if (modClearingAddr != null || modScanning) return
        modClearingAddr = address
        modError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val fresh = brand.clear(ctrl, address)
                withContext(Dispatchers.Main) {
                    modReport = modReport?.let { r ->
                        r.copy(modules = r.modules.map { m ->
                            if (m.address == address) m.copy(dtcs = fresh, error = null) else m
                        })
                    }
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (controller === ctrl) modError = e.message ?: "Clear failed."
                }
            } finally {
                withContext(Dispatchers.Main) { modClearingAddr = null }
            }
        }
    }

    /** Run one of the selected brand's extra actions (extras / snapshot / …). */
    fun runBrandAction(action: BrandAction) {
        val ctrl = controller ?: run {
            modError = "Connect to the car first (Car tab)."
            return
        }
        if (extrasBusy || modScanning) return
        setLive(false) // retargets the ELM; pause the dashboard
        val brand = moduleBrand
        extrasBusy = true
        extrasRunningId = action.id
        modError = ""
        scope.launch(Dispatchers.IO) {
            try {
                val readings = action.run(ctrl) { step ->
                    scope.launch(Dispatchers.Main) { modProgress = step }
                }
                withContext(Dispatchers.Main) {
                    extras = readings
                    extrasRead = true
                    extrasSource = action.id
                    extrasBrand = brand
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    if (controller === ctrl) modError = e.message ?: "Read failed."
                }
            } finally {
                withContext(Dispatchers.Main) {
                    extrasBusy = false
                    extrasRunningId = null
                    modProgress = ""
                }
            }
        }
    }

    // ---------------- service resets (oil / brake / inspection) -------------
    // Reuses moduleBrand as the selected brand. The catalog is static (no car
    // needed to browse it); running a uds reset needs the car connected.
    var resetRunningId by mutableStateOf<String?>(null)
    var resetMessage by mutableStateOf("")
    var resetProgress by mutableStateOf("")
    var resetLog by mutableStateOf<List<String>>(emptyList())

    /** The resets available for the currently-selected module brand. */
    fun resetsForBrand(): List<ServiceReset> = ServiceResets.forBrand(moduleBrand)

    /** Run one UDS service reset over the connected dongle. Confirm-gated by UI. */
    fun runReset(reset: ServiceReset) {
        if (!reset.uds) return // procedures are display-only; UI shows the steps
        val ctrl = controller ?: run {
            resetMessage = "Connect to the car first (Car tab)."
            return
        }
        if (resetRunningId != null || modScanning || extrasBusy) return
        setLive(false) // a reset retargets the ELM; pause the dashboard
        resetRunningId = reset.id
        resetMessage = ""
        resetLog = emptyList()
        resetProgress = "Starting ${reset.name}…"
        scope.launch(Dispatchers.IO) {
            try {
                val outcome = ctrl.performReset(reset) { step ->
                    scope.launch(Dispatchers.Main) { resetProgress = step }
                }
                withContext(Dispatchers.Main) {
                    resetMessage = outcome.message
                    resetLog = outcome.log
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    resetMessage = e.message ?: "Reset failed."
                }
            } finally {
                withContext(Dispatchers.Main) {
                    resetRunningId = null
                    resetProgress = ""
                }
            }
        }
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
