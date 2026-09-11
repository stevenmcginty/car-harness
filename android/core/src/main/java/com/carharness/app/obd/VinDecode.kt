package com.carharness.app.obd

/**
 * Offline VIN decode: manufacturer (from the 3-char WMI) + the model year
 * (from position 10) — but only where that maker verifiably encodes a year
 * there. One shared table so the Car tab, the Modules hero and the scan
 * reports all headline the car the same way — no network calls, no
 * manufacturer-proprietary model decode (the VDS in positions 4-9 is not
 * public), so make + trustworthy year is the honest limit.
 *
 * A year at position 10 is a US/Canada legal requirement (49 CFR 565), NOT
 * part of the ISO VIN standard, and much of what a UK dealer sees puts
 * something else there:
 *  - Mazda Europe (JMZ): a serial digit — a 2016 MX-5 ND reads '1' = "2001".
 *  - Mercedes: EU VINs use Daimler's own scheme, position 10 is not a year.
 *  - Ford Europe (WF0): a model-code LETTER sits at 10 (Ford's build year and
 *    month live at 11/12 in Ford's own code) — decodes to a confident wrong year.
 *  - PSA and Fiat/Alfa: the year is only encoded on modern cars (letter codes,
 *    2010+); a digit at 10 is pre-2010 serial noise.
 *  - EU-built BMW/MINI, Toyota and Nissan park '0'/'U' (invalid codes) at 10,
 *    which safely refuse below.
 * So every WMI carries a year rule: FULL (trust any valid code), LETTERS_ONLY
 * (modern letter codes only), or NONE (never a year there). A wrong year on a
 * dealer's screen is worse than no year — when the rule refuses, marketNote
 * says so instead of guessing.
 */
data class VehicleIdentity(
    val vin: String? = null,       // the raw VIN as read from the car
    val make: String? = null,      // manufacturer from the WMI, null if not in the table
    val year: Int? = null,         // model year from position 10 where genuinely encoded
    val marketNote: String? = null, // honest "no year in this VIN format" note
)

object VinDecode {

    /** Full decode: make + year where trustworthy, with the VIN echoed back. */
    fun decode(vin: String?): VehicleIdentity {
        if (vin == null) return VehicleIdentity()
        val make = makeFromVin(vin)
        val year = yearFromVin(vin)
        val note =
            if (make != null && year == null && vin.length >= 10)
                "Model year isn't encoded in this VIN format (normal on EU-market cars), " +
                    "so none is shown rather than a wrong one."
            else null
        return VehicleIdentity(vin = vin, make = make, year = year, marketNote = note)
    }

    /** Manufacturer from the VIN's world-manufacturer identifier (first 3 chars). */
    fun makeFromVin(vin: String?): String? = makerFor(vin)?.make

    /**
     * Model year from VIN position 10, only where this WMI's rule allows it;
     * null when the char isn't a year code or the maker doesn't encode one.
     * Unknown WMIs starting 1-5 are North-America-built, where the year code
     * is required by law, so those still decode.
     */
    fun yearFromVin(vin: String?): Int? {
        if (vin == null || vin.length < 10) return null
        val c = vin.uppercase()[9]
        val year = YEAR[c] ?: return null
        val rule = makerFor(vin)?.rule
            ?: if (vin[0] in '1'..'5') YearRule.FULL else return null
        return when (rule) {
            YearRule.FULL -> year
            YearRule.LETTERS_ONLY -> if (c.isLetter()) year else null
            YearRule.NONE -> null
        }
    }

    private enum class YearRule { FULL, LETTERS_ONLY, NONE }
    private data class Maker(val make: String, val rule: YearRule)

    private fun makerFor(vin: String?): Maker? {
        val wmi = vin?.take(3)?.uppercase() ?: return null
        return WMI[wmi]
    }

    // Exact 3-char WMI map, unknown -> null. Covers the makes a UK dealer sees;
    // multiple prefixes per make where a brand splits plants/regions, each with
    // the year rule that market's VIN format has actually been verified to use.
    private val WMI: Map<String, Maker> = buildMap {
        fun all(rule: YearRule, make: String, vararg wmis: String) =
            wmis.forEach { put(it, Maker(make, rule)) }

        // NA-market Mazdas encode the year by law; JMZ (Europe) and the other
        // export prefixes carry a serial digit at 10 — field-proven wrong
        // ("likely 2001" on a 2016 ND).
        all(YearRule.FULL, "Mazda", "JM1", "JM3", "3MD", "3MZ")
        all(YearRule.NONE, "Mazda", "JMZ", "JM7", "JM0")
        // BMW/MINI encode the year worldwide EXCEPT EU-built cars, which park
        // '0' at position 10 — not a valid code, so those refuse safely.
        all(YearRule.FULL, "BMW", "WBA", "WBS", "WBY", "WBW", "4US")
        all(YearRule.FULL, "MINI", "WMW")
        // Mercedes EU VINs use Daimler's own scheme — position 10 is not a
        // year (a UK RHD car would decode as 2002-ish junk). US-market cars do
        // encode, but these WMIs don't split by market, so honesty wins.
        all(
            YearRule.NONE, "Mercedes-Benz",
            "WDB", "WDD", "WDC", "W1K", "W1N", "W1V", "WMX",
        )
        // The VW group's worldwide scheme genuinely puts the year at 10.
        all(YearRule.FULL, "Volkswagen", "WVW", "WVG", "WV1", "WV2", "3VW", "1VW", "9BW")
        all(YearRule.FULL, "Audi", "WAU", "WA1", "WUA", "TRU")
        all(YearRule.FULL, "Škoda", "TMB")
        all(YearRule.FULL, "SEAT", "VSS")
        all(YearRule.FULL, "Porsche", "WP0", "WP1")
        // PSA encodes the year only from ~2009 on (letter codes = 2010+); a
        // digit at 10 is pre-2010 serial noise, so digits refuse.
        all(YearRule.LETTERS_ONLY, "Peugeot", "VF3", "VR3")
        all(YearRule.LETTERS_ONLY, "Citroën", "VF7", "VR7")
        all(YearRule.LETTERS_ONLY, "DS Automobiles", "VR1")
        // Renault's EU VINs keep a serial there — never verified as a year.
        all(YearRule.NONE, "Renault", "VF1")
        // Fiat group: US-market cars encode (letters); EU cars have serial
        // digits or '0' at 10, so only letters are trusted.
        all(YearRule.LETTERS_ONLY, "Fiat", "ZFA")
        all(YearRule.LETTERS_ONLY, "Alfa Romeo", "ZAR")
        all(YearRule.LETTERS_ONLY, "Lancia", "ZLA")
        all(YearRule.FULL, "Jeep", "1J4", "1J8")
        // Ford of Europe puts a model-code LETTER at 10 that decodes to a
        // confident wrong year; their build year/month live at 11/12 in
        // Ford's own letter code, which isn't decoded here.
        all(YearRule.NONE, "Ford", "WF0")
        // Ford Australia/Thailand (Ranger T6, MAJ/6FP) uses the same
        // model-code-at-10 scheme as Europe, so those refuse too.
        all(YearRule.NONE, "Ford", "MAJ", "6FP")
        // North-American Fords do encode the year letter at 10, by law.
        all(YearRule.FULL, "Ford", "1FA", "1FT", "1FM", "2FM", "3FA")
        // Opel has used the model-year letter at 10 since the '80s.
        all(YearRule.FULL, "Opel / Vauxhall", "W0L", "W0V")
        all(YearRule.FULL, "Vauxhall", "VXK")
        // Japan-built Toyotas/Hondas/Nissans follow the year code; EU-built or
        // EU-market cars carry '0' (Toyota) or the market code 'U' (Nissan) at
        // 10 — both invalid year codes, so they refuse safely.
        all(YearRule.FULL, "Toyota", "JTD", "JTE", "JTH", "JTM", "SB1")
        all(YearRule.FULL, "Honda", "JHM", "SHH")
        all(YearRule.FULL, "Nissan", "JN1", "JN3", "JN8", "SJN")
        // North-American and Mexican Nissans encode the year letter by law.
        all(YearRule.FULL, "Nissan", "1N4", "1N6", "5N1", "3N1")
        // Infiniti is Nissan's own diagnostic bus, so Brands.forMake sends it
        // to the Nissan walker. JN1 stays Nissan: it carries both.
        all(YearRule.FULL, "Infiniti", "JNK", "JNR")
        all(YearRule.FULL, "Hyundai", "KMH")
        all(YearRule.FULL, "Kia", "KNA", "KND")
        all(YearRule.FULL, "Volvo", "YV1", "YV4")
        all(YearRule.FULL, "Jaguar", "SAJ")
        all(YearRule.FULL, "Land Rover", "SAL")
        all(YearRule.FULL, "Tesla", "5YJ", "LRW", "XP7")
    }

    // Position-10 year code -> year, resolved to 2001-2030 (skips I/O/Q/U/Z, 0).
    private val YEAR: Map<Char, Int> = buildMap {
        "123456789".forEachIndexed { i, c -> put(c, 2001 + i) }
        "ABCDEFGHJKLMNPRSTVWXY".forEachIndexed { i, c -> put(c, 2010 + i) }
    }
}
