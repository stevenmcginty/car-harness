package com.carharness.app.obd

/**
 * The per-brand service-reset definitions. Mirror of the desktop's
 * backend/profiles/<brand>/resets.py — KEEP BOTH IN SYNC. Provenance and
 * confidence grades come from docs/private/service-resets-research.md.
 *
 * BMW/Mazda UDS resets are filled from the CBS / SkyActiv research; Peugeot is
 * dash procedures because PSA's UDS reset zone isn't publicly documented.
 */
object BrandResetData {

    // BMW / MINI CBS. The dongle reset (0x2E to KOMBI 0x60) needs one BMW byte
    // that isn't public yet, so this is the verified dash method; see
    // backend/profiles/bmw/resets.py for the staged 0x2E machinery.
    val BMW: List<ServiceReset> = listOf(
        ServiceReset(
            id = "bmw-cbs-dash",
            brand = "bmw",
            name = "CBS service reset (oil / brakes / vehicle check)",
            description = "Resets a Condition Based Service item from the dash — engine oil, " +
                "brake fluid, front/rear pads, vehicle check. The dongle route needs one " +
                "manufacturer byte that isn't public yet, so this is the on-cluster method, " +
                "which needs no tools and is what BMW's own menu does.",
            uds = false,
            confidence = "verified",
            source = "BMW F-series CBS reset (BC-stalk method); ISTA CBS_RESET job",
            models = "F-series BMW + UKL MINI (F20/F30/F26 X4, F54/F55/F56/F60), ~2011–2019",
            cautions = listOf(
                "Scroll to the specific item (oil, brakes…) before confirming — you reset " +
                    "one item at a time.",
                "Only reset an item you've actually serviced; the cluster records the reset " +
                    "date and mileage.",
            ),
            procedure = listOf(
                "Get in, don't start the engine. Press Start/Stop once WITHOUT a foot on the " +
                    "brake to wake the car (ignition on).",
                "Press and hold the BC button on the end of the indicator stalk until the " +
                    "service (CBS) menu appears in the cluster.",
                "Short-press the BC button to step to the item you serviced (e.g. Engine oil).",
                "Press and hold the BC button again — a 'Reset?' prompt appears.",
                "Confirm with another press/hold. The item returns to 100% and the date/mileage " +
                    "are stamped.",
                "Switch the ignition off to finish.",
            ),
        ),
    )

    // Mazda SkyActiv — dash/pedal procedures (no public dongle reset frame).
    val MAZDA: List<ServiceReset> = listOf(
        ServiceReset(
            id = "mazda-maintenance-monitor",
            brand = "mazda",
            name = "Maintenance monitor reset (oil / service / tyre rotation)",
            description = "Resets the SkyActiv 'wrench' maintenance monitor after a service. It " +
                "tracks Scheduled Maintenance, Oil Change and Tyre Rotation — there's no dongle " +
                "command for it, so this is the built-in dash reset.",
            uds = false,
            confidence = "verified",
            source = "Mazda owner's manuals; Service Alert SA-071/17",
            models = "SkyActiv MX-5 ND, 2/DJ, 3/BM/BN/BP, 6/GJ/GL, CX-3, CX-5 KE/KF",
            cautions = listOf(
                "With no centre display (Type A) the trip and average economy reset too if a " +
                    "trip is on screen — clear it first.",
                "After resetting, cycle the ignition and check the wrench is actually gone.",
            ),
            procedure = listOf(
                "Cars WITH the centre display (MZD Connect):",
                "  1. Ignition ON, engine off (press Start twice, no brake).",
                "  2. Home/Applications → Vehicle Status Monitor → Maintenance.",
                "  3. Pick the item (Oil Change / Scheduled Maintenance / Tyre Rotation) → " +
                    "Reset → Yes.",
                "  4. Ignition off.",
                "Cars WITHOUT a centre display (base trims):",
                "  1. Ignition OFF.",
                "  2. Hold the trip/selector button, then switch the ignition ON, keeping it held.",
                "  3. Keep holding 7+ seconds until the master warning light flashes. Release.",
            ),
        ),
        ServiceReset(
            id = "mazda-ieloop-battery",
            brand = "mazda",
            name = "Battery / i-ELOOP reset after battery change",
            description = "After replacing the battery on an i-stop / i-ELOOP car the charging " +
                "system has to relearn. No dongle reset — this is Mazda's official pedal " +
                "procedure, done with the OLD battery still fitted.",
            uds = false,
            confidence = "verified",
            source = "Mazda Service Bulletin 01-002/14; i-stop initialisation (WSM)",
            models = "SkyActiv i-stop / i-ELOOP (Mazda3, Mazda6, CX-5)",
            cautions = listOf(
                "On i-ELOOP cars disconnect the CURRENT SENSOR connector BEFORE the negative " +
                    "terminal, or you'll get false readings / DTCs.",
                "Do the pedal steps before swapping the battery, then reinitialise windows, " +
                    "sunroof, clock and TPMS after.",
            ),
            procedure = listOf(
                "With the old battery still fitted: ignition ON (engine off), gear in N/P, foot " +
                    "on the brake.",
                "Depress the accelerator fully for 5 seconds or more.",
                "Check the charging-system and master warning lights flash.",
                "Depress and release the accelerator 3 times.",
                "Check the charging light comes on steady and the master warning light goes out.",
                "Ignition OFF, change the battery (sensor connector first on i-ELOOP), reconnect, " +
                    "then reinitialise windows/sunroof/clock/TPMS.",
            ),
        ),
        ServiceReset(
            id = "mazda-diesel-oil-data",
            brand = "mazda",
            name = "Engine oil data reset (SkyActiv-D diesel)",
            description = "SkyActiv-D diesels hold an oil-deterioration figure in the PCM that " +
                "must be reset at every oil change. FORScan can do it over the port but the " +
                "routine id isn't public, so this is Mazda's official no-tool jumper method.",
            uds = false,
            confidence = "community",
            source = "Mazda WSM 'Engine oil data reset [SkyActiv-D 2.2]'; FORScan",
            models = "SkyActiv-D 1.5 / 2.2 diesels (UK/EU stock)",
            cautions = listOf(
                "Diesel only — petrol SkyActiv has no PCM oil-data counter.",
                "Reset AFTER the oil + filter change, not before.",
            ),
            procedure = listOf(
                "Do the oil and filter change first.",
                "Ignition ON, engine off.",
                "Jumper the diagnostic test terminal (TEN) to body ground.",
                "Within 5 seconds, fully depress and release the accelerator 5 times.",
                "The glow-plug indicator flashes 5 times = reset done.",
                "Remove the jumper. (Or with FORScan: Service Functions → Reset Engine Oil " +
                    "Deterioration Learned Value on the PCM.)",
            ),
        ),
    )

    // Peugeot / PSA — dash procedures (no public dongle route for the reset).
    val PEUGEOT: List<ServiceReset> = listOf(
        ServiceReset(
            id = "peugeot-service",
            brand = "peugeot",
            name = "Service indicator (spanner) reset",
            description = "Clears the service-due spanner after an oil/interval service. On PSA " +
                "cars the counter lives in the BSI; there's no public dongle command, so this " +
                "is the official trip-button procedure — no tools, under a minute.",
            uds = false,
            confidence = "verified",
            source = "Peugeot owner's manuals; klavkarr/mrautofixer reset guides",
            models = "208/2008, 308, 3008/5008, 508 and Citroen/DS equivalents (~2010+)",
            cautions = listOf(
                "If you disconnect the battery afterwards, lock the car and wait 5 minutes " +
                    "first, or the reset is lost.",
            ),
            procedure = listOf(
                "Switch the ignition OFF.",
                "Press and hold the trip-distance zero-reset button (end of the wiper stalk, " +
                    "the lighting stalk, or below the speedometer).",
                "Switch the ignition ON while still holding the button.",
                "The mileage display starts counting down; keep holding.",
                "When it shows '=0' (or the digital cluster says 'Re-initialisation performed'), " +
                    "release the button.",
                "The spanner is gone. Switch the ignition off to finish.",
            ),
        ),
        ServiceReset(
            id = "peugeot-dpf-additive",
            brand = "peugeot",
            name = "DPF / Eolys additive top-up reset (HDi diesel)",
            description = "After topping up the Eolys additive on an HDi diesel, the additive " +
                "counter is reset in the engine ECU. This is a named DiagBox function whose " +
                "routine id isn't public, so it's documented here as a specialist step.",
            uds = false,
            confidence = "community",
            source = "peugeotforums DiagBox additive-reset threads",
            models = "HDi / BlueHDi diesels with an Eolys additive tank",
            cautions = listOf(
                "Only needed after refilling additive or replacing the DPF.",
                "Requires DiagBox/Lexia; a generic dongle can't perform this.",
            ),
            procedure = listOf(
                "Connect DiagBox/Lexia (not a generic dongle).",
                "Repair and test by ECU -> Engine Management ECU -> Repair -> Pack Repair -> " +
                    "Replacement Parts -> Filling of the Additive Reservoir.",
                "Enter the current mileage in km and follow the prompts.",
                "If the DPF itself was replaced, also run the 'changing FAP' step so the soot " +
                    "counters are reset.",
                "Switch the ignition off then on to store.",
            ),
        ),
    )
}
