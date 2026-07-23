package com.carharness.app.obd

/**
 * Offline VIN decode: manufacturer (from the 3-char WMI) + the likely model
 * year (from position 10). One shared table so the Car tab, the Modules hero
 * and the scan reports all headline the car the same way — no network calls,
 * no manufacturer-proprietary model decode (the VDS in positions 4-9 is not
 * public), so make + year is the honest limit.
 *
 * Position-10 year: EU VINs are not legally required to encode the year there,
 * but every make we sell does, so it is presented as the *likely* year, not a
 * guarantee. The code table skips I/O/Q/U/Z and 0 and resolves to the
 * 2001-2030 window (the 30-year cycle repeats, but nothing here is that old).
 */
data class VehicleIdentity(
    val vin: String? = null,       // the raw VIN as read from the car
    val make: String? = null,      // manufacturer from the WMI, null if not in the table
    val year: Int? = null,         // likely model year from position 10, null if undecodable
    val marketNote: String? = null,
)

object VinDecode {

    /** Full decode: make + likely year, with the VIN echoed back. */
    fun decode(vin: String?): VehicleIdentity {
        if (vin == null) return VehicleIdentity()
        return VehicleIdentity(vin = vin, make = makeFromVin(vin), year = yearFromVin(vin))
    }

    /** Manufacturer from the VIN's world-manufacturer identifier (first 3 chars). */
    fun makeFromVin(vin: String?): String? {
        val wmi = vin?.take(3)?.uppercase() ?: return null
        return WMI[wmi]
    }

    /** Likely model year from VIN position 10; null if the char isn't a year code. */
    fun yearFromVin(vin: String?): Int? {
        if (vin == null || vin.length < 10) return null
        return YEAR[vin.uppercase()[9]]
    }

    // Exact 3-char WMI map, unknown -> null. Covers the makes a UK dealer sees;
    // multiple prefixes per make where a brand splits plants/regions.
    private val WMI: Map<String, String> = buildMap {
        listOf("JMZ", "JM1", "JM3", "JM7", "JM0", "3MD", "3MZ").forEach { put(it, "Mazda") }
        listOf("WBA", "WBS", "WBY", "WBW", "4US").forEach { put(it, "BMW") }
        put("WMW", "MINI")
        listOf("WDB", "WDD", "WDC", "W1K", "W1N", "W1V", "WMX").forEach { put(it, "Mercedes-Benz") }
        listOf("WVW", "WVG", "WV1", "WV2", "3VW", "1VW", "9BW").forEach { put(it, "Volkswagen") }
        listOf("WAU", "WA1", "WUA", "TRU").forEach { put(it, "Audi") }
        put("TMB", "Škoda")
        put("VSS", "SEAT")
        listOf("WP0", "WP1").forEach { put(it, "Porsche") }
        listOf("VF3", "VR3").forEach { put(it, "Peugeot") }
        listOf("VF7", "VR7").forEach { put(it, "Citroën") }
        put("VR1", "DS Automobiles")
        put("VF1", "Renault")
        put("ZFA", "Fiat")
        put("ZAR", "Alfa Romeo")
        put("ZLA", "Lancia")
        listOf("1J4", "1J8").forEach { put(it, "Jeep") }
        put("WF0", "Ford")
        listOf("W0L", "W0V").forEach { put(it, "Opel / Vauxhall") }
        put("VXK", "Vauxhall")
        listOf("JTD", "JTE", "JTH", "JTM", "SB1").forEach { put(it, "Toyota") }
        listOf("JHM", "SHH").forEach { put(it, "Honda") }
        listOf("JN1", "SJN").forEach { put(it, "Nissan") }
        put("KMH", "Hyundai")
        listOf("KNA", "KND").forEach { put(it, "Kia") }
        listOf("YV1", "YV4").forEach { put(it, "Volvo") }
        put("SAJ", "Jaguar")
        put("SAL", "Land Rover")
        listOf("5YJ", "LRW", "XP7").forEach { put(it, "Tesla") }
    }

    // Position-10 year code -> year, resolved to 2001-2030 (skips I/O/Q/U/Z, 0).
    private val YEAR: Map<Char, Int> = buildMap {
        "123456789".forEachIndexed { i, c -> put(c, 2001 + i) }
        "ABCDEFGHJKLMNPRSTVWXY".forEachIndexed { i, c -> put(c, 2010 + i) }
    }
}
