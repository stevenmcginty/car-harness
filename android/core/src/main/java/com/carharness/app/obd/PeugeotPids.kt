package com.carharness.app.obd

/**
 * Peugeot / PSA (Citroen, DS) health-check values readable over a plain ELM327
 * on the diagnostic CAN — the dealer data generic OBD-II doesn't carry. PSA
 * exposes this two different ways depending on ECU generation, and this file
 * models both:
 *
 *  - UDS service 0x22 (`22 XXXX` -> `62 XXXX <data>`): newer ECUs (BSI2010 UDS,
 *    EDC17, NAC). One DID = one value. -> [PsaDid], [DIDS].
 *  - KWP service 0x21 (`21 XX8001` -> `61 XX8001 <block>`): older HDi engines
 *    (Delphi DCM3.5, Bosch EDC16). ONE request returns a whole measurement
 *    block; each value sits at a byte offset into it. -> [PsaBlock], [BLOCKS].
 *
 * Neither needs a security unlock or a non-default session for reads.
 *
 * Sources: OBDb PSA signalsets (per-model, exact scaling), the DW10CD/DCM3.5
 * Diagbox trace on dispatchexpertscudo.org.uk (the 0x21 blocks), ludwig-v's
 * arduino-psa-diag and prototux/PSA-RE (id pairs, sessions, standard DIDs).
 * Entries are best-effort: a value this car/engine doesn't support never
 * answers (or reads out of its sanity range) and is dropped — unverified
 * entries are harmless. Read-only. Mirror of backend/profiles/peugeot/pids.py.
 */

/** UDS service-0x22 single value: payload after `62 <did>` -> display string. */
class PsaDid(
    val reqId: Int,
    val did: Int,
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/** One value extracted from a KWP 0x21 block payload (byte A = index 0). */
class PsaField(
    val name: String,
    val unit: String,
    val decode: (ByteArray) -> String?,
)

/**
 * A KWP service-0x21 block read. `localId` is the 3-byte local identifier
 * (e.g. "CB8001"); [fields] decode the block payload AFTER the `61 <localId>`
 * echo, so field offsets are 0-based from the first data byte ("A").
 */
class PsaBlock(
    val reqId: Int,
    val localId: String,
    val label: String,
    val fields: List<PsaField>,
)

/** An odometer source, decoded straight to kilometres for the cross-check. */
class OdoSource(
    val reqId: Int,
    val did: Int,
    val label: String,
    val decodeKm: (ByteArray) -> Double?,
)

object PeugeotPids {

    // ------------------------------------------------------------- byte helpers
    // For 0x21 blocks the letter offsets map A=0, B=1, ... Z=25, AA=26, ...

    private fun u8(b: ByteArray, i: Int): Int? = if (b.size > i) b[i].toInt() and 0xFF else null

    private fun u16(b: ByteArray, i: Int): Int? =
        if (b.size > i + 1) ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF) else null

    private fun s16(b: ByteArray, i: Int): Int? {
        val v = u16(b, i) ?: return null
        return if (v > 0x7FFF) v - 0x10000 else v
    }

    private fun u24(b: ByteArray, i: Int): Int? =
        if (b.size > i + 2) ((b[i].toInt() and 0xFF) shl 16) or
            ((b[i + 1].toInt() and 0xFF) shl 8) or (b[i + 2].toInt() and 0xFF) else null

    private fun u32(b: ByteArray, i: Int): Long? =
        if (b.size > i + 3) ((b[i].toLong() and 0xFF) shl 24) or
            ((b[i + 1].toLong() and 0xFF) shl 16) or ((b[i + 2].toLong() and 0xFF) shl 8) or
            (b[i + 3].toLong() and 0xFF) else null

    private fun fmt(v: Double, dp: Int): String = "%.${dp}f".format(v)

    /** u16 at index [i] -> scaled string, or null outside [lo]..[hi]. */
    private fun u16At(
        i: Int, scale: Double = 1.0, offset: Double = 0.0, dp: Int = 0,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        u16(b, i)?.let { val v = it * scale + offset; if (v in lo..hi) fmt(v, dp) else null }
    }

    /** s16 at index [i] -> scaled string, or null outside [lo]..[hi]. */
    private fun s16At(
        i: Int, scale: Double = 1.0, offset: Double = 0.0, dp: Int = 0,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        s16(b, i)?.let { val v = it * scale + offset; if (v in lo..hi) fmt(v, dp) else null }
    }

    /** u8 at index [i] -> scaled string, or null outside [lo]..[hi]. */
    private fun u8At(
        i: Int, scale: Double = 1.0, offset: Double = 0.0, dp: Int = 0,
        lo: Double = Double.NEGATIVE_INFINITY, hi: Double = Double.POSITIVE_INFINITY,
    ): (ByteArray) -> String? = { b ->
        u8(b, i)?.let { val v = it * scale + offset; if (v in lo..hi) fmt(v, dp) else null }
    }

    // ------------------------------------------------------------- UDS 0x22 DIDs
    // e-CMP HV battery, on the BMS at 0x6B4 -> 0x694. From OBDb's Corsa-e
    // signalset (the one real PSA-family dataset there); the same e-CMP battery
    // is in the e-208 / e-2008 / e-C4 / DS3 E-Tense. State of Charge is verified
    // against real Peugeot captures (e-208/e-2008/e-3008); the rest are Corsa-e
    // defined, medium confidence — a non-EV simply never answers, so harmless.
    // Battery State of Health is the used-EV appraisal headline.
    val DIDS: List<PsaDid> = listOf(
        // D860 SOH: fmt bix 8 (skip 1 byte), len 16, div 16, max 100.
        PsaDid(0x6B4, 0xD860, "HV battery health (SOH)", "%", u16At(1, 1.0 / 16, dp = 1, lo = 0.0, hi = 100.0)),
        // D410 SOC (calibrated): len 16, div 512, max 100. VERIFIED on real cars.
        PsaDid(0x6B4, 0xD410, "HV battery charge (SOC)", "%", u16At(0, 1.0 / 512, dp = 1, lo = 0.0, hi = 100.0)),
        // D815 HV pack voltage: len 16, div 16.
        PsaDid(0x6B4, 0xD815, "HV battery voltage", "V", u16At(0, 1.0 / 16, dp = 1, lo = 0.0, hi = 4095.0)),
        // D865 energy stored: len 16, div 64, unit kWh.
        PsaDid(0x6B4, 0xD865, "HV energy stored", "kWh", u16At(0, 1.0 / 64, dp = 2, lo = 0.0, hi = 200.0)),
        // D86F / D870 min & max cell voltage: len 16, unit mV.
        PsaDid(0x6B4, 0xD86F, "HV min cell voltage", "mV", u16At(0, lo = 0.0, hi = 5000.0)),
        PsaDid(0x6B4, 0xD870, "HV max cell voltage", "mV", u16At(0, lo = 0.0, hi = 5000.0)),
        // EP6/THP oil circuit on the engine (0x6A8). VLud + 208 GTi Diagbox
        // telemetry. A diesel/non-THP simply NAKs.
        PsaDid(0x6A8, 0xD429, "Engine oil pressure", "bar",
            u16At(0, 0.00016, dp = 2, lo = 0.0, hi = 10.6)),
        PsaDid(0x6A8, 0xD47D, "Engine oil temperature", "°C",
            s16At(0, 0.25, dp = 1, lo = -40.0, hi = 200.0)),
        PsaDid(0x6A8, 0xD4D6, "Engine oil temperature (estimated)", "°C",
            s16At(0, 0.25, dp = 1, lo = -40.0, hi = 200.0)),
        PsaDid(0x6A8, 0xD41D, "Oil pressure sensor voltage", "mV",
            u16At(0, 0.2, dp = 0, lo = 0.0, hi = 6000.0)),
    )

    // ---------------------------------------------------------- KWP 0x21 blocks
    // Verified for the Delphi DCM3.5 / DW10CD (2.0 BlueHDi) family from a
    // Diagbox trace; MEDIUM confidence and engine-family specific — a different
    // engine simply won't answer these local ids, or the values fall out of the
    // sanity ranges and are dropped. The DPF block is the dealer headline: it
    // tells you whether a diesel's particulate filter is healthy.
    val BLOCKS: List<PsaBlock> = listOf(
        PsaBlock(
            0x6A8, "CB8001", "exhaust / DPF",
            listOf(
                // MP_MASSE_TOTALE_SUIE_DANS_FAP = INT16(J:K)/1000 g  (J = index 9)
                PsaField("DPF soot load", "g", u16At(9, 0.001, dp = 1, lo = 0.0, hi = 120.0)),
                // MP_PRESSION_DIFFERENTIELLE_FAP = INT16(P:Q) mbar   (P = index 15)
                PsaField("DPF differential pressure", "mbar", u16At(15, lo = 0.0, hi = 2000.0)),
                // MP_TEMPERATURE_FAP = INT16(Z:AA) °C                (Z = index 25)
                PsaField("DPF temperature", "°C", u16At(25, lo = -50.0, hi = 1000.0)),
                // MP_DISTANCE_DEPUIS_DERNIERE_REGEN = INT16(AC:AD) km (AC = index 28)
                PsaField("Distance since last regen", "km", u16At(28, lo = 0.0, hi = 65000.0)),
                // DISTANCE_MOYENNE_ENTRE_10_DERNIERES_REGEN = INT16(AE:AF) km (index 30)
                PsaField("Avg distance between regens", "km", u16At(30, lo = 0.0, hi = 65000.0)),
                // VOLADDRESERVOIR (Eolys additive tank) = INT16(T:U) raw (T = index 19)
                PsaField("Additive (Eolys) tank", "", u16At(19, lo = 0.0, hi = 65535.0)),
            )
        ),
        PsaBlock(
            0x6A8, "C98001", "injection",
            listOf(
                // MP_REGIME_MOTEUR = INT16(A:B) rpm                  (A = index 0)
                PsaField("Engine RPM", "rpm", u16At(0, lo = 0.0, hi = 8000.0)),
                // MP_PRESSION_CARBURANT_MESURE = INT16(M:N)/10 bar   (M = index 12)
                PsaField("Rail pressure (measured)", "bar", u16At(12, 0.1, dp = 1, lo = 0.0, hi = 3000.0)),
                // MP_TEMP_CARB_C9 = AC-50 °C                         (AC = index 28)
                PsaField("Fuel temperature", "°C", u8At(28, offset = -50.0, lo = -40.0, hi = 150.0)),
            )
        ),
        PsaBlock(
            0x6A8, "CA8001", "air / turbo",
            listOf(
                // MP_PRESSION_TURBO_MESUREE = INT16(I:J) mbar        (I = index 8)
                PsaField("Turbo pressure (measured)", "mbar", u16At(8, lo = 0.0, hi = 4000.0)),
                // MP_CONSIGNE_PRESSION_TURBO = INT16(G:H) mbar       (G = index 6)
                PsaField("Turbo pressure (setpoint)", "mbar", u16At(6, lo = 0.0, hi = 4000.0)),
            )
        ),
        PsaBlock(
            0x6A8, "CD8001", "coolant",
            listOf(
                // MP_TEMPERATURE_EAU_b = D-50 °C                     (D = index 3)
                PsaField("Coolant temperature", "°C", u8At(3, offset = -50.0, lo = -40.0, hi = 150.0)),
            )
        ),
        PsaBlock(
            0x6A8, "CC8001", "electrical",
            listOf(
                // MP_TENSION_BATTERIE1 = P/10 V                      (P = index 15)
                PsaField("Battery voltage (engine ECU)", "V", u8At(15, 0.1, dp = 1, lo = 5.0, hi = 16.0)),
            )
        ),
    )

    // Odometer, from the e-CMP BMS at 0x6B4 (DID D49C: len 32, div 1000, km).
    // This is the ONLY documented PSA odometer read — the pre-2021 diesel/petrol
    // BSI/cluster mileage has no clean public DID, so the module-to-module
    // clocking cross-check only fires when 2+ sources answer (EVs today); on a
    // diesel this list is silent and the VIN cross-check in the scan is the
    // fraud check that works. On an EV, compare this against the dash.
    val ODO_SOURCES: List<OdoSource> = listOf(
        OdoSource(0x6B4, 0xD49C, "HV battery module") { b -> u32(b, 0)?.let { it / 1000.0 } },
    )
}
