package com.carharness.app

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult as BleScanResult
import android.content.Context
import android.content.Intent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.setValue
import androidx.core.content.FileProvider
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
import com.carharness.app.obd.JlrAdBlue
import com.carharness.app.obd.JlrPids
import com.carharness.app.obd.LiveReading
import com.carharness.app.obd.ObdController
import com.carharness.app.obd.ObdTransport
import com.carharness.app.obd.Pids
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
import java.io.BufferedWriter
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

sealed class ConnState {
    data object Idle : ConnState()
    data object Connecting : ConnState()
    data class Connected(val label: String, val firmware: String, val voltage: String) : ConnState()
    data class Error(val message: String) : ConnState()
}

data class AdapterRow(val id: String, val name: String, val typeId: String)
data class ModuleRow(val id: String, val name: String, val profile: String, val builtin: Boolean)

/** Where one leg of the one-tap job got to. */
enum class StepState { Pending, Running, Done, Failed, Skipped }

/**
 * One leg of the one-tap job. [note] is the honest detail under the label —
 * what it found, or why it couldn't. Skipped is not Failed: a car that won't
 * answer its VIN, or a brand the dongle can't walk, is a wall we name rather
 * than an error we blame the user for.
 */
data class JobStep(
    val id: String,
    val label: String,
    val state: StepState = StepState.Pending,
    val note: String = "",
)

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

    // ---- remembered dongle -------------------------------------------------
    // The whole point of one-tap: the second time you use the app it already
    // knows which dongle is yours, so "Scan this car" needs no setup at all.
    private val prefs = context.getSharedPreferences("car_harness_ui", Context.MODE_PRIVATE)
    var lastAddress: String? = prefs.getString("last_address", null)
        private set
    var lastAddressName: String = prefs.getString("last_address_name", "") ?: ""
        private set

    /**
     * Only ever called after a link actually came up. Remembering a dongle
     * that failed would make the next "Scan this car" charge confidently at
     * an address that has never worked.
     */
    private fun rememberTransport(kind: String, address: String?) {
        val name = (bleDevices + bondedDevices).firstOrNull { it.second == address }?.first
        val editor = prefs.edit()
            .putString("last_transport", kind)
            .putString("last_address", address)
        if (name != null) editor.putString("last_address_name", name)
        editor.apply()
        lastAddress = address
        if (name != null) lastAddressName = name
    }

    /** True when one tap can go straight to the car with no picking about. */
    fun canAutoConnect(): Boolean = when (transportChoice) {
        "demo", "wifi" -> true
        else -> lastAddress != null
    }

    init {
        // Come back up on whatever transport last worked, not on "demo".
        prefs.getString("last_transport", null)?.let { transportChoice = it }
    }

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
        scope.launch { connectAwait(kind, address) }
    }

    /**
     * The connect the one-tap job waits on. Same work as [connect], but it
     * suspends until the link is either up (true) or refused (false) so the
     * scan sequence can decide whether to carry on.
     */
    suspend fun connectAwait(kind: String, address: String? = null): Boolean {
        if (connState is ConnState.Connecting) return false
        stopBleScan()
        disconnect()
        connState = ConnState.Connecting
        transportChoice = kind
        return withContext(Dispatchers.IO) {
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
                                        "off. Reconnect from Home."
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
                        applyBrandFromVin(ident?.make)
                    }
                    // The link came up, so this dongle is worth remembering —
                    // tomorrow's "Scan this car" then needs no setup at all.
                    rememberTransport(kind, address)
                }
                true
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
                false
            }
        }
    }

    /**
     * Point the manufacturer profile at whatever the VIN says the car is. Only
     * moves when we actually recognise the make — an unrecognised WMI leaves
     * the user's own choice standing rather than guessing at it.
     */
    private fun applyBrandFromVin(make: String?) {
        val brand = Brands.forMake(make) ?: return
        if (brand.id != moduleBrand) {
            moduleBrand = brand.id
            modError = ""
        }
        moduleBrandFromVin = true
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
        liveSupported = null
        liveCatalogBusy = false
        modReport = null
        modError = ""
        modProgress = ""
        extras = emptyList()
        extrasRead = false
        extrasBrand = ""
        // The CSV files stay on disk; only the in-memory handle to the last
        // one goes, with the card that offered its Share button.
        endDriveLog()
        driveLogFile = null
        driveLogSavedTo = null
        resetMessage = ""
        resetProgress = ""
        resetLog = emptyList()
    }

    // ---------------- live data ----------------
    var liveOn by mutableStateOf(false)
    var readings by mutableStateOf<List<LiveReading>>(emptyList())
    var liveError by mutableStateOf("")
    private var liveJob: Job? = null

    /** Which values the dashboard polls — persisted, defaulting to the classic
     * Torque-style ten. Kept small on purpose: over a BLE dongle every extra
     * value slows the whole refresh. */
    var liveSelected by mutableStateOf(
        prefs.getStringSet("live_keys", null)?.toSet() ?: Pids.dashboardKeys.toSet()
    )
        private set

    /** What this car reports supporting (null until asked). */
    var liveSupported by mutableStateOf<Set<String>?>(null)
    var liveCatalogBusy by mutableStateOf(false)

    fun toggleLiveKey(key: String) {
        liveSelected = if (key in liveSelected) liveSelected - key else liveSelected + key
        prefs.edit().putStringSet("live_keys", liveSelected).apply()
    }

    fun setLiveKeys(keys: Collection<String>, on: Boolean) {
        liveSelected = if (on) liveSelected + keys else liveSelected - keys.toSet()
        prefs.edit().putStringSet("live_keys", liveSelected).apply()
    }

    /** Ask the car which standard values it supports, so the picker can show
     * this exact vehicle rather than the whole SAE table. */
    fun loadLiveCatalog() {
        val ctrl = controller ?: return
        if (liveCatalogBusy || liveSupported != null) return
        liveCatalogBusy = true
        scope.launch(Dispatchers.IO) {
            val keys = try {
                ctrl.liveSupportedKeys()
            } catch (_: Exception) {
                null
            }
            withContext(Dispatchers.Main) {
                if (controller === ctrl) liveSupported = keys
                liveCatalogBusy = false
            }
        }
    }

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
                    // Catalogue order, so tiles keep a stable, sensible order
                    // whatever order the values were ticked in.
                    val wanted = Pids.all.filter { it.key in liveSelected }.map { it.key }
                        .ifEmpty { Pids.dashboardKeys }
                    val r = ctrl.live(wanted)
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
            faultError = "Connect to the car first — the button on Home does it for you."
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
    /** Set when the VIN chose the brand, so the picker can say "FROM VIN". */
    var moduleBrandFromVin by mutableStateOf(false)
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

    // ---- drive-log capture (JLR today) -------------------------------------
    // Each tick streams to a CSV in the app's own external-files dir as it
    // lands, so the farm-day dataset survives a crash, a dropped dongle or
    // closing the app mid-run. No storage permission needed (app-private
    // dir); sharing goes through the FileProvider declared in the manifest.
    // Sim runs write too, with a sim- filename prefix and a # SIMULATED
    // header line — the share path is exactly what must not turn out broken
    // on the day, and the label keeps demo data unmistakable.
    var driveLogFile by mutableStateOf<File?>(null)
        private set
    var driveLogSavedTo by mutableStateOf<String?>(null)
        private set

    /** Drive-log length picker, seconds. A regen needs ~20 min (1500 s+). */
    var driveLogSeconds by mutableStateOf(180L)
    /** DPF + EGR watch: log only the aftertreatment candidate subset. */
    var driveLogDpfEgrOnly by mutableStateOf(false)
    private var driveLogWriter: BufferedWriter? = null
    private var driveLogDids: List<Int>? = null
    private var driveLogExtras: List<String>? = null

    /** Reset the capture before a run: close any previous file, forget it. */
    private fun startDriveLog() {
        endDriveLog()
        driveLogFile = null
        driveLogSavedTo = null
    }

    /** Append one tick. Called on the IO coroutine the drive log runs on;
     *  every line is flushed so a dead battery costs at most one tick. */
    private fun appendDriveSample(s: JlrAdBlue.DriveSample) {
        val ctrl = controller ?: return
        try {
            var writer = driveLogWriter
            if (writer == null) {
                val dir = context.getExternalFilesDir(null) ?: context.cacheDir
                val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
                val name = (if (ctrl.simulated) "sim-" else "") + "jlr-drive-log-$stamp.csv"
                val file = File(dir, name)
                writer = file.outputStream().bufferedWriter()
                if (ctrl.simulated) writer.write("# SIMULATED — no real car connected\n")
                driveLogWriter = writer
                driveLogFile = file
                driveLogSavedTo = file.absolutePath
            }
            var dids = driveLogDids
            if (dids == null) {
                // Column order is fixed by the first tick's DID set (the log
                // polls one fixed list, so every tick carries the same keys)
                // plus whatever aftertreatment values that first tick answered
                // — a value that only answers later is absent from the sheet
                // but still on the progress lines.
                dids = s.dids.keys.sorted()
                driveLogDids = dids
                driveLogExtras = s.extras.keys.sorted()
                val header = buildString {
                    append("t,engine_rpm,coolant_c,module_voltage")
                    for (d in dids) append(",did_%04X".format(d))
                    for (k in driveLogExtras.orEmpty()) append(",$k")
                }
                writer.write(header + "\n")
            }
            val cells = mutableListOf(
                "%.3f".format(Locale.US, s.t),
                s.rpm?.let { "%.0f".format(Locale.US, it) } ?: "",
                s.coolant?.let { "%.1f".format(Locale.US, it) } ?: "",
                s.voltage?.let { "%.2f".format(Locale.US, it) } ?: "",
            )
            for (d in dids) cells.add(s.dids[d] ?: "")
            for (k in driveLogExtras.orEmpty()) {
                val v = s.extras[k]
                cells.add(if (v != null) "%.1f".format(Locale.US, v) else "")
            }
            writer.write(cells.joinToString(",") + "\n")
            writer.flush()
        } catch (_: IOException) {
            // Disk trouble must never take the diagnostic itself down.
        }
    }

    private fun endDriveLog() {
        try {
            driveLogWriter?.close()
        } catch (_: IOException) {
        }
        driveLogWriter = null
        driveLogDids = null
        driveLogExtras = null
    }

    /** A share intent for the captured CSV, or null when there is nothing
     *  (yet) to hand over. Caller wraps it in Intent.createChooser. */
    fun shareDriveLogCsvIntent(): Intent? {
        val file = driveLogFile ?: return null
        if (!file.isFile) return null
        return try {
            val uri = FileProvider.getUriForFile(
                context, context.packageName + ".fileprovider", file,
            )
            Intent(Intent.ACTION_SEND).apply {
                type = "text/csv"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        } catch (_: IllegalArgumentException) {
            // No provider path covers the file — better no share than a crash.
            null
        }
    }

    fun chooseModuleBrand(brand: String) {
        // Picking by hand always beats the VIN guess — and stops the picker
        // claiming "FROM VIN" over a choice the user made themselves.
        moduleBrandFromVin = false
        if (brand == moduleBrand) return
        moduleBrand = brand
        modError = ""
    }

    fun runModuleScan() {
        val ctrl = controller ?: run {
            modError = "Connect to the car first — the button on Home does it for you."
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
            modError = "Connect to the car first — the button on Home does it for you."
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
                val onStep: (String) -> Unit = { step ->
                    scope.launch(Dispatchers.Main) { modProgress = step }
                }
                // The JLR drive log is the one action that also CAPTURES: each
                // tick streams to the on-disk CSV as it lands, so the run is a
                // dataset the moment it happens (see the drive-log block above).
                val readings =
                    if (brand == "jlr" && action.id == "drivelog") {
                        startDriveLog()
                        try {
                            ctrl.jlrAdblueDriveLog(
                                onStep, driveLogSeconds, ::appendDriveSample,
                                if (driveLogDpfEgrOnly) JlrPids.DPF_EGR_WATCH_DIDS else null,
                            )
                        } finally {
                            endDriveLog()
                        }
                    } else {
                        action.run(ctrl, onStep)
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
            resetMessage = "Connect to the car first — the button on Home does it for you."
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

    // ---------------- the one-tap job ----------------
    // Everything a dealer actually does to a car on the forecourt, in the
    // order they'd do it, off one button: link up, find out what the car is,
    // read the generic codes, then walk the manufacturer's own modules. Each
    // leg reports for itself so a wall in the middle never looks like a crash.

    var jobRunning by mutableStateOf(false)
        private set
    var jobDone by mutableStateOf(false)
        private set
    var jobSteps by mutableStateOf<List<JobStep>>(emptyList())
        private set
    /** Set when the job can't start on its own and needs the user to set up. */
    var jobNeedsSetup by mutableStateOf(false)
        private set
    private var jobJob: Job? = null

    private fun setStep(id: String, state: StepState, note: String = "") {
        jobSteps = jobSteps.map {
            if (it.id == id) it.copy(state = state, note = note) else it
        }
    }

    fun cancelFullScan() {
        jobJob?.cancel()
        jobJob = null
        jobRunning = false
        modScanning = false
        scanning = false
        modProgress = ""
        jobSteps = jobSteps.map {
            if (it.state == StepState.Running) it.copy(state = StepState.Skipped, note = "Stopped")
            else it
        }
    }

    fun runFullScan() {
        if (jobRunning) return
        jobRunning = true
        jobDone = false
        jobNeedsSetup = false
        jobSteps = listOf(
            JobStep("link", "Connect to the car"),
            JobStep("id", "Identify the car"),
            JobStep("faults", "Read fault codes"),
            JobStep("modules", "Deep scan every module"),
        )
        jobJob = scope.launch {
            try {
                // ---- 1. link ------------------------------------------------
                if (connState !is ConnState.Connected) {
                    if (!canAutoConnect()) {
                        setStep(
                            "link", StepState.Failed,
                            "No dongle picked yet — choose it below, then tap again.",
                        )
                        jobNeedsSetup = true
                        return@launch
                    }
                    setStep(
                        "link", StepState.Running,
                        if (lastAddressName.isNotEmpty()) "Waking $lastAddressName…"
                        else "Opening the link…",
                    )
                    if (!connectAwait(transportChoice, lastAddress)) {
                        setStep(
                            "link", StepState.Failed,
                            (connState as? ConnState.Error)?.message
                                ?: "Couldn't reach the dongle.",
                        )
                        jobNeedsSetup = true
                        return@launch
                    }
                }
                val cs = connState as? ConnState.Connected
                setStep(
                    "link", StepState.Done,
                    listOfNotNull(cs?.label, cs?.voltage?.ifEmpty { null })
                        .joinToString(" · "),
                )

                // ---- 2. identity --------------------------------------------
                setStep("id", StepState.Running, "Asking the car for its VIN…")
                var v = vehicle
                if (v == null) {
                    v = withContext(Dispatchers.IO) {
                        try {
                            controller?.identify()
                        } catch (_: Exception) {
                            null
                        }
                    }
                    vehicle = v
                    applyBrandFromVin(v?.make)
                }
                if (v?.vin != null) {
                    setStep(
                        "id", StepState.Done,
                        listOfNotNull(v.make, v.year?.let { "likely $it" }, v.vin)
                            .joinToString(" · "),
                    )
                } else {
                    setStep(
                        "id", StepState.Skipped,
                        "The car didn't answer the VIN request — is the ignition on?",
                    )
                }

                val ctrl = controller ?: run {
                    setStep("faults", StepState.Failed, "Link dropped before the scan.")
                    return@launch
                }
                setLive(false) // the scan must not fight the dashboard for the ELM

                // ---- 3. generic fault codes ---------------------------------
                setStep("faults", StepState.Running, "Reading stored and pending codes…")
                scanning = true
                try {
                    val r = withContext(Dispatchers.IO) { ctrl.scan() }
                    scanResult = r
                    val n = r.stored.size + r.pending.size + r.permanent.size
                    setStep(
                        "faults", StepState.Done,
                        when {
                            n == 0 && r.milOn -> "No codes, but the engine light is ON"
                            n == 0 -> "No fault codes"
                            n == 1 -> "1 fault code"
                            else -> "$n fault codes"
                        },
                    )
                } catch (e: Exception) {
                    faultError = e.message ?: "Scan failed."
                    setStep("faults", StepState.Failed, faultError)
                } finally {
                    scanning = false
                }

                // ---- 4. manufacturer deep scan ------------------------------
                val brand = Brands.byId(moduleBrand)
                if (brand == null) {
                    setStep("modules", StepState.Skipped, "No manufacturer profile selected.")
                    return@launch
                }
                setStep("modules", StepState.Running, "Walking ${brand.label} control units…")
                modScanning = true
                modError = ""
                try {
                    val onStep: (String) -> Unit = { step ->
                        scope.launch { modProgress = step }
                    }
                    val report = withContext(Dispatchers.IO) { brand.scan(ctrl, onStep) }
                    modReport = report
                    val faulted = report.modules.count { it.dtcs.isNotEmpty() }
                    val found = report.modules.count { it.present }
                    if (!report.ok) {
                        modError = report.message
                        setStep("modules", StepState.Skipped, report.message)
                    } else {
                        setStep(
                            "modules", StepState.Done,
                            "$found modules answered · " +
                                if (faulted == 0) "all clear" else "$faulted holding faults",
                        )
                    }
                } catch (e: Exception) {
                    modError = e.message ?: "Module scan failed."
                    setStep("modules", StepState.Failed, modError)
                } finally {
                    modScanning = false
                    modProgress = ""
                }
            } finally {
                jobRunning = false
                jobDone = jobSteps.any { it.state != StepState.Pending }
                jobJob = null
            }
        }
    }

    /** Every fault the job turned up — generic codes plus per-module ones. */
    fun jobFaultTotal(): Int {
        val generic = scanResult?.let {
            it.stored.size + it.pending.size + it.permanent.size
        } ?: 0
        val perModule = modReport?.modules?.sumOf { it.dtcs.size } ?: 0
        return generic + perModule
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
