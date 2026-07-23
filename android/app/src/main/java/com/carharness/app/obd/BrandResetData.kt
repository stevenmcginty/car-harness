package com.carharness.app.obd

/**
 * The per-brand service-reset definitions. Mirror of the desktop's
 * backend/profiles/<brand>/resets.py — KEEP BOTH IN SYNC. Provenance and
 * confidence grades come from docs/private/service-resets-research.md.
 *
 * BMW/Mazda UDS resets are filled from the CBS / SkyActiv research; Peugeot and
 * VW/Audi are dash procedures because neither PSA's BSI reset zone nor VAG's
 * 'Resetting ESI' cluster adaptation is publicly documented.
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

    // VW / Audi (VAG) — dash / menu procedures. The UDS 'Resetting ESI'
    // adaptation on cluster 0x714 isn't publicly documented (research §6), so
    // every VAG reset ships as a procedure. Mirror of backend/profiles/vw/resets.py
    // — KEEP BOTH IN SYNC.
    val VW: List<ServiceReset> = listOf(
        ServiceReset(
            id = "vw-sri-button",
            brand = "vw",
            name = "Oil / inspection service reset (button cluster)",
            description = "Resets the Service Reminder Interval on VAG cars with a trip-reset " +
                "(0.0) button in the cluster. There's no public dongle command for the UDS " +
                "'Resetting ESI' adaptation, so this is the built-in button-through-ignition " +
                "procedure.",
            uds = false,
            confidence = "verified",
            source = "Golf 6/7 oil-service reset guides (scegliauto, seventrumpet, erwinsalarda, " +
                "carworklog MK7, golfmk7 forum)",
            models = "Most 2008–2019 VAG with a button cluster — Golf 6/7 CONFIRMED; same family " +
                "on Polo, Passat, Tiguan, T-Roc, Škoda, SEAT",
            cautions = listOf(
                "Directly confirmed on Golf 6/7. On other models the wording and hold-time vary " +
                    "— check the owner's manual wording matches before relying on it.",
                "Reset AFTER the oil + filter change, not before.",
            ),
            procedure = listOf(
                "Ignition OFF.",
                "Press and hold the trip-reset (0.0) button in the cluster.",
                "Switch the ignition ON, keeping the button held.",
                "\"RESET OIL CHANGE SERVICE?\" appears — for the longer INSPECTION service " +
                    "instead, keep holding ~10–20 s until \"RESET INSPECT. SERVICE?\" shows.",
                "Release the button.",
                "Press the button again to confirm — \"SERVICE WAS RESET\" is shown.",
                "Ignition OFF.",
            ),
        ),
        ServiceReset(
            id = "vw-sri-menu",
            brand = "vw",
            name = "Service reset via digital cockpit / MMI menu",
            description = "Cars with a full TFT digital cockpit or central touchscreen (Golf 8 " +
                "and most 2019+ VAG, Audi MMI) reset the service reminder from the on-screen " +
                "menu rather than the trip button.",
            uds = false,
            confidence = "community",
            source = "Ross-Tech SRI reset wiki; VAG digital-cockpit / MMI service menu " +
                "(family pattern, per-model wording varies)",
            models = "Golf 8+ and 2019+ VAG with digital cockpit; Audi MMI (A3 8Y, A4 B9, Q3, …)",
            cautions = listOf(
                "The exact menu wording and path vary by model and MMI/cockpit generation — " +
                    "confirm against the car's own menu.",
                "Some early builds still honour the button-through-ignition method.",
            ),
            procedure = listOf(
                "Ignition ON, engine off.",
                "Open the infotainment / cluster menu: Car (or Vehicle) → Service (& Inspection).",
                "Select the service item you replaced (e.g. Oil change / Inspection).",
                "Choose Reset and confirm.",
                "Ignition OFF, then back ON and check the service message is gone.",
            ),
        ),
    )

    // Jaguar / Land Rover — dash procedures throughout (no public dongle reset
    // frame; research docs/private/jlr-research-resets.md). Dash procedures
    // don't care about the MY17+ DoIP wall, so these work on EVERY era.
    val JLR: List<ServiceReset> = listOf(
        ServiceReset(
            id = "jlr-oil-xf-x250",
            brand = "jlr",
            name = "Oil service reset (Jaguar XF X250 2008-15) — fog-switch dance",
            description = "Resets the service-interval reminder on the Jaguar XF X250 with " +
                "the rear-fog-switch sequence. Fiddly but real — no dongle command exists " +
                "for any JLR service reset, so this is the built-in method.",
            uds = false,
            confidence = "verified",
            source = "scegliauto 132196; jaguarforums X250 threads; enginepartsdiagram",
            models = "Jaguar XF X250 (2008-2015)",
            cautions = listOf(
                "Reset AFTER the oil + filter change, not before.",
                "The switch sequence must be done briskly (within ~3 s) — memorise it first.",
            ),
            procedure = listOf(
                "Ignition ON without touching the brake (no engine).",
                "Hold the REAR FOG LAMP switch, press START (still no brake), release the " +
                    "fog switch.",
                "Now hold the trip-computer button (end of the stalk) AND the rear fog " +
                    "switch together until the message centre shows 'resetting service " +
                    "mode' then 'service mode reset'.",
                "Release both, ignition OFF. Restart and check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-modern-jaguar",
            brand = "jlr",
            name = "Oil service reset (Jaguar XE / XF X260 / F-Pace) — pedals or menu",
            description = "Resets the service reminder on the 2015-on Jaguars. Two built-in " +
                "routes: the bonnet-open pedal dance, or the steering-wheel Service menu " +
                "where fitted. No dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "scegliauto 107146/132196; fpaceforums 7582/5393; justanswer F-Pace",
            models = "Jaguar XE (X760), XF (X260), F-Pace (X761), 2015-2021",
            cautions = listOf(
                "Reset AFTER the oil + filter change, not before.",
                "2021+ facelift cars: sources are split — try brake ~3 s then accelerator " +
                    "~10 s until 'service counter resetting'; some 2021+ cars only reset " +
                    "with dealer/JLR software.",
                "Manual resets on modern JLR often clear the oil counter but not the " +
                    "time/days counter — if a date-based reminder returns, it needs a tool.",
            ),
            procedure = listOf(
                "Route A (pedals): bonnet OPEN, all doors closed, then open the driver's " +
                    "door. Ignition ON without the brake (no engine).",
                "Press brake AND accelerator fully together and hold ~10 s until the " +
                    "cluster shows 'service resetting'.",
                "Release, ignition OFF then ON to confirm the message is gone.",
                "Route B (menu, where fitted): steering-wheel arrows → Service / Oil " +
                    "Change → hold OK/reset until it confirms.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-evoque-l538",
            brand = "jlr",
            name = "Oil service reset (Range Rover Evoque L538) — wheel menu",
            description = "Resets the service reminder on the 2011-2018 Evoque through the " +
                "steering-wheel cluster menu. No dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "scegliauto 106248/32686; seventrumpet; Land Rover SSM60837",
            models = "Range Rover Evoque L538 (2011-2018)",
            cautions = listOf(
                "Reset AFTER the oil + filter change, not before.",
                "The facelift/L551 Evoque moves to the bonnet-open pedal method (see the " +
                    "Disco Sport entry).",
            ),
            procedure = listOf(
                "Ignition ON (no engine).",
                "Steering-wheel OK button → Vehicle Information → Oil Service.",
                "Press RES (lower-centre wheel button) to reset; alternatively Settings → " +
                    "Service menu, hold the left-stalk-end button ~10 s.",
                "Ignition OFF then ON and check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-disco-sport",
            brand = "jlr",
            name = "Oil service reset (Discovery Sport L550) — bonnet + pedals",
            description = "Resets the service reminder on the 2014-2019 Discovery Sport " +
                "with the bonnet-open pedal dance. No dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "seventrumpet; discosportforums 14079; GAP diagnostic notes",
            models = "Discovery Sport L550 (2014-2019); same method on late Evoque",
            cautions = listOf(
                "Reset AFTER the oil + filter change, not before.",
                "KNOWN LIMITATION: on some model years the cluster won't clear the " +
                    "message even after a valid reset — that needs an IPC software update " +
                    "or a JLR tool, not a repeat of the procedure.",
            ),
            procedure = listOf(
                "Bonnet OPEN, close all doors, ignition ON without the brake (no engine), " +
                    "then open the driver's door.",
                "Press brake AND accelerator fully together and hold ~30 s until the " +
                    "cluster shows 'resetting' then 'reset complete'.",
                "Release, ignition OFF then ON and check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-disco3",
            brand = "jlr",
            name = "Service interval reset (Discovery 3 / LR3) — trip-button hold",
            description = "Resets the service-interval (SIA) reminder on the 2004-2009 " +
                "Discovery 3 with the classic cluster-button hold. No dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "4x4community 111739; scegliauto 108490; enginepartsdiagram",
            models = "Discovery 3 / LR3 (2004-2009)",
            cautions = listOf(
                "Reset AFTER the service, not before.",
            ),
            procedure = listOf(
                "Ignition OFF.",
                "Press and HOLD the left cluster (trip) reset button.",
                "Turn the ignition to position II, keeping the button held ~5 s until " +
                    "the LCD shows 'RESET SIA'.",
                "Release, then press the button again to confirm ('rst').",
                "Ignition OFF, restart, check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-disco4",
            brand = "jlr",
            name = "Oil service reset (Discovery 4 / LR4) — pedals (or trip button)",
            description = "Resets the service reminder on the 2010-2016 Discovery 4. Two " +
                "variants exist across the run — the pedal dance and the earlier " +
                "trip-button hold; try the pedals first. No dongle command exists.",
            uds = false,
            confidence = "community",
            source = "aulro 286061; enginepartsdiagram; scegliauto (variants disputed by MY)",
            models = "Discovery 4 / LR4 (2010-2016)",
            cautions = listOf(
                "Sources genuinely disagree on which model years take which variant — if " +
                    "the pedal route doesn't take, try the Disco 3 trip-button hold.",
                "Reset AFTER the service, not before.",
            ),
            procedure = listOf(
                "Bonnet OPEN, driver's door open, ignition ON (no engine).",
                "Hold accelerator AND brake fully together until the cluster responds.",
                "Cycle the trip display to END then TRIP to exit.",
                "Ignition OFF, start the engine to confirm the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-rrs-l320",
            brand = "jlr",
            name = "Oil service reset (Range Rover Sport L320) — cruise CANCEL menu",
            description = "Resets the service reminder on the 2005-2013 RR Sport through " +
                "the cluster menu with the cruise CANCEL double-press. No dongle command " +
                "exists.",
            uds = false,
            confidence = "verified",
            source = "rangerovers.net 17848/277762; seventrumpet",
            models = "Range Rover Sport L320 (2005-2013)",
            cautions = listOf(
                "Post-2010 diesels may need diagnostics if the menu route refuses.",
                "Reset AFTER the service, not before.",
            ),
            procedure = listOf(
                "Ignition ON, engine OFF.",
                "Steering-wheel controls → Service Menu → Oil Level Display (or Vehicle " +
                    "Info → Service Interval on some years).",
                "Press cruise CANCEL twice within 2 seconds (or hold reset on the " +
                    "Service Interval screen).",
                "Ignition OFF then ON and check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-rrs-l494",
            brand = "jlr",
            name = "Oil service reset (Range Rover Sport L494) — bonnet + pedals",
            description = "Resets the service reminder on the 2013-2022 RR Sport with the " +
                "modern bonnet-open pedal dance (or the cluster menu where fitted). No " +
                "dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "scegliauto 103663; rangerovers.net; seventrumpet",
            models = "Range Rover Sport L494 (2013-2022)",
            cautions = listOf(
                "Reset AFTER the oil + filter change, not before.",
            ),
            procedure = listOf(
                "Bonnet OPEN, doors closed, then open the driver's door. Ignition ON " +
                    "without the brake (no engine).",
                "Press brake AND accelerator fully together and hold until the cluster " +
                    "shows 'service resetting'.",
                "Release, ignition OFF then ON to confirm.",
                "Menu alternative (where fitted): Vehicle Info → Service Interval → " +
                    "hold reset.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-l405",
            brand = "jlr",
            name = "Oil service reset (Range Rover L405) — pedals (MY14+, petrol)",
            description = "Resets the service reminder on the full-size L405 with the " +
                "pedal dance. Real caveats: it works from MY14 on, and several sources " +
                "say PETROL only — diesel L405s (and pre-MY14 cars) need a JLR-capable " +
                "tool.",
            uds = false,
            confidence = "verified",
            source = "rangerovers.net 110361/277762; landroverworld 25667",
            models = "Range Rover L405 (MY14-2022) — petrol; diesel/pre-MY14 = tool only",
            cautions = listOf(
                "Pre-MY14 cars: no dash route exists — tool only.",
                "Diesel: multiple sources report the pedal reset doesn't take — expect " +
                    "to need a JLR-capable tool (GAP IIDTool / iCarsoft LR / SDD).",
                "Reset AFTER the oil + filter change, not before.",
            ),
            procedure = listOf(
                "Bonnet OPEN, doors closed, then open the driver's door. Ignition ON " +
                    "without the brake (no engine).",
                "Press brake AND accelerator fully together and hold ~10 s until the " +
                    "cluster shows 'service resetting'.",
                "Release, ignition OFF then ON to confirm.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-l322",
            brand = "jlr",
            name = "Service reset (Range Rover L322) — three systems by year",
            description = "The L322 changed reset systems twice across one badge. " +
                "2002-2005 (BMW-era) and 2006-2009 (Ford-era) both use a trip-button " +
                "hold with different displays; the 2010-2013 facelift has NO dash route " +
                "at all — tool only (IIDTool/SDD).",
            uds = false,
            confidence = "community",
            source = "scegliauto 42778; aulro 205836; enginepartsdiagram (variants by year)",
            models = "Range Rover L322: 2002-05 and 2006-09 dash; 2010-13 TOOL ONLY",
            cautions = listOf(
                "2010-2013 facelift: genuinely tool-only — don't keep trying the buttons.",
                "Year variants are community-sourced and disputed — if one display " +
                    "doesn't match, try the other era's steps.",
            ),
            procedure = listOf(
                "2002-2005 (BMW-era): ignition OFF. Hold the LEFT trip button, turn the " +
                    "ignition to position I, keep held ~5 s until 'OIL SERVICE' or " +
                    "'INSPECTION' shows with a reset prompt, confirm.",
                "2006-2009: hold the trip button, ignition to position II, 'SERVICE' " +
                    "flashes, keep held ~5 s until it shows 'END'/resets.",
                "2010-2013: no dash procedure exists — use a GAP IIDTool, iCarsoft LR " +
                    "or SDD.",
            ),
        ),
        ServiceReset(
            id = "jlr-oil-freelander2",
            brand = "jlr",
            name = "Service reset (Freelander 2 / LR2) — cluster button + start",
            description = "Resets the maintenance-interval reminder on the 2006-2014 " +
                "Freelander 2 with the cluster-button hold. Very well documented. No " +
                "dongle command exists.",
            uds = false,
            confidence = "verified",
            source = "scegliauto 65052/54476/46795; resetservicelight; multiple videos agree",
            models = "Freelander 2 / LR2 (2006-2014), key-start and push-button variants",
            cautions = listOf(
                "Reset AFTER the service, not before.",
            ),
            procedure = listOf(
                "Press and HOLD the reset button below the speedometer.",
                "With it held, insert the key / press START (ignition on).",
                "Keep holding until the cluster asks 'reset maintenance interval?'.",
                "Hold (or press) the button again to confirm.",
                "Ignition OFF then ON and check the reminder is gone.",
            ),
        ),
        ServiceReset(
            id = "jlr-epb-modern",
            brand = "jlr",
            name = "Rear brake pads — EPB service mode (modern motor-on-caliper JLR)",
            description = "Modern JLR (Evoque, Disco Sport, RR Sport L494, L405, Velar, " +
                "Jaguar XE/XF X260/F-Pace) has an electric park brake motor ON the rear " +
                "caliper. To change rear pads the motors must wind back in SERVICE MODE — " +
                "and there is a no-tool switch dance for it. The critical warning: " +
                "forcing the piston back WITHOUT service mode destroys the in-caliper " +
                "actuator.",
            uds = false,
            confidence = "verified",
            source = "iCarsoft LR EPB guide; rangerovers.net 342310; evoqueforums 6696; " +
                "xeforums 6363 (switch position disputed — both recorded)",
            models = "Evoque (late L538/L551), Discovery Sport L550, RR Sport L494, " +
                "RR L405, Velar, Jaguar XE / XF X260 / F-Pace — REAR pads only",
            cautions = listOf(
                "NEVER compress a rear piston on these cars without service mode — it " +
                    "destroys the in-caliper EPB actuator (expensive).",
                "Sources disagree whether the switch is held in RELEASE (down) or APPLY " +
                    "(up) in step 3 — if one doesn't take, try the other.",
                "If the dance won't take at all, the tool route (e.g. iCarsoft LR " +
                    "'EPB → Pad Replacement') is the fallback.",
                "Wind the screw-type piston back with an EPB wind-back tool, not a " +
                    "C-clamp.",
            ),
            procedure = listOf(
                "Make sure the park brake is RELEASED (ignition on; pull the EPB switch " +
                    "while pressing the brake if needed — red PARK icon must clear).",
                "Ignition ON via START without the brake (no engine).",
                "Hold the EPB switch ~2 s (release position; try apply if it won't take).",
                "Hold the accelerator to FULL throttle ~2 s and KEEP IT HELD.",
                "Cycle the ignition off/on (two START presses) while still holding.",
                "Listen for the rear motors winding back; the cluster shows 'park brake " +
                    "in service mode'.",
                "Do the pad job (wind-back tool on the screw piston).",
                "To exit: hold EPB switch (apply) + accelerator ~2 s and cycle the " +
                    "ignition — or it exits automatically on the first engine start.",
            ),
        ),
        ServiceReset(
            id = "jlr-epb-cable",
            brand = "jlr",
            name = "EPB service mode + the famous gearset trap (Disco 3/4, RRS L320, L322)",
            description = "These cars use a cable-pulled drum-in-hat park brake: the " +
                "CALIPER is conventional (C-clamp fine for pads), but disc/shoe work " +
                "needs the cable slackened in service mode — a no-tool dance. The famous " +
                "trap: the EPB actuator gearset burns out because of WORN SHOES / " +
                "STRETCHED CABLES; replacing the actuator alone re-fails.",
            uds = false,
            confidence = "verified",
            source = "roverparts LR3 notes; landroverforums 61830; iscantech; roverparts.eu",
            models = "Discovery 3/4, Range Rover Sport L320, Range Rover L322",
            cautions = listOf(
                "One of the most misdiagnosed faults on these cars: an EPB motor that " +
                    "'failed' usually burned out from worn shoes/stretched cables. " +
                    "Service shoes AND cables together or the new actuator dies the " +
                    "same death.",
                "Do NOT start the engine mid-procedure — service mode cancels on the " +
                    "first engine start.",
                "Rear PADS need no service mode on these cars (conventional caliper).",
            ),
            procedure = listOf(
                "Ignition ON (no engine).",
                "Hold the EPB switch in APPLY, wait ~2 s.",
                "Hold the accelerator to full throttle, wait ~2 s (keep both held).",
                "Cycle the ignition off/on, then release — an audible cable release " +
                    "confirms service mode.",
                "Do the drum/shoe/cable work.",
                "Exit: normal EPB apply after the job (or first engine start).",
            ),
        ),
        ServiceReset(
            id = "jlr-battery-bms",
            brand = "jlr",
            name = "Battery replacement / BMS registration — what to know (tool job)",
            description = "JLR cars from ~2010 run a Battery Management System with a " +
                "smart alternator. After a battery swap the BMS must be told — and that " +
                "write lives in the BCM, which no generic dongle reaches. There is NO " +
                "no-tool route; this card is the honest brief so the swap is done right.",
            uds = false,
            confidence = "verified",
            source = "iCarsoft BMS guide; premium-diagnostics; ANCEL/Nexas coverage docs",
            models = "All JLR ~2010+ with BMS/smart charging (most of the range)",
            cautions = listOf(
                "Skipping registration: the alternator charges the new battery as if it " +
                    "were the old worn one — overcharges and cooks it, shortens its " +
                    "life, and can disable stop-start.",
                "Fit a same-spec AGM battery of the correct Ah — not a standard flooded " +
                    "battery.",
            ),
            procedure = listOf(
                "Fit a same-spec AGM battery of the correct capacity (check the label " +
                    "on the old one).",
                "Have the new battery REGISTERED with a JLR-capable tool (GAP IIDTool, " +
                    "iCarsoft LR, Autel, or SDD/Pathfinder) — there is no dash or dongle " +
                    "route.",
                "Symptoms if it was skipped: battery/charge warning, short new-battery " +
                    "life, stop-start dead.",
            ),
        ),
        ServiceReset(
            id = "jlr-dpf-drive-cycle",
            brand = "jlr",
            name = "DPF regeneration drive cycle (Ingenium / TDV6 diesels)",
            description = "If the DPF-full light shows on an otherwise healthy diesel, a " +
                "passive on-road regeneration usually completes with a sustained run — " +
                "the legitimate first move before reaching for a tool. Forced/static " +
                "regen is tool-only AND a workshop procedure (hot exhaust, stationary).",
            uds = false,
            confidence = "verified",
            source = "kmotors 2.0D DPF; rcv.co.uk; westminstermotors; discosportforums",
            models = "Ingenium 2.0D (XE/XF/F-Pace/Evoque/Disco Sport), TDV6/SDV6 diesels",
            cautions = listOf(
                "If the car is in limp mode or the light is flashing/red, stop — that " +
                    "stage needs a tool (forced regen) or a workshop, not a drive.",
                "Sources disagree on an rpm requirement — the sustained speed at " +
                    "operating temperature is the part everyone agrees on.",
                "Short urban runs are what filled the DPF — one drive cycle won't fix " +
                    "a car that only ever does the school run.",
            ),
            procedure = listOf(
                "Warm the engine to normal operating temperature.",
                "Drive 20-30 minutes at a steady 50-70 mph on an A-road or motorway.",
                "The light should clear during or shortly after the run.",
                "If it returns quickly or won't clear: differential-pressure sensor, " +
                    "frequent-regen faults or a genuinely full filter — tool/workshop " +
                    "time.",
            ),
        ),
        ServiceReset(
            id = "jlr-tpms-relearn",
            brand = "jlr",
            name = "TPMS relearn (existing sensors) — auto-learn drive",
            description = "JLR predominantly auto-learns its tyre-pressure sensors: after " +
                "a rotation or pressure correction, a steady drive re-learns the IDs " +
                "with no tool. NEW sensors are different — registering new IDs needs a " +
                "TPMS activation tool.",
            uds = false,
            confidence = "verified",
            source = "mytpms JLR; tirereview; ateq-tpms (speed/time variants recorded)",
            models = "JLR ~2005+ with factory TPMS",
            cautions = listOf(
                "Replacing a SENSOR (new IDs) is tool territory — this relearn only " +
                    "re-associates existing sensors.",
                "Sources vary on speed/time — the conservative version below covers " +
                    "them all.",
            ),
            procedure = listOf(
                "Set all tyres (including the spare where it has a sensor) to the " +
                    "door-placard pressures.",
                "Drive at a steady 25-30 mph or above for at least 15 minutes.",
                "The warning clears as the IDs re-learn; if it persists, a sensor is " +
                    "dead or new IDs need a tool.",
            ),
        ),
        ServiceReset(
            id = "jlr-eas-transport-mode",
            brand = "jlr",
            name = "Air suspension: calibration & transport mode — what to know (tool job)",
            description = "Two honest warnings for air-sprung JLR. (1) Ride-height " +
                "CALIBRATION after suspension work is tool-only — getting it wrong means " +
                "wrong heights, lockout or airspring damage. (2) A bought-in/auction car " +
                "can arrive in TRANSPORT MODE: suspension works but wipers, locks, " +
                "interior lights or speedo act dead and the fob won't lock — that's not " +
                "an electrical fault, and the exit needs JLR-approved diagnostics.",
            uds = false,
            confidence = "verified",
            source = "iCarsoft EAS calibration guide; roverparts iLand notes; dealer PDI reports",
            models = "Air-sprung Land Rovers (Disco 3/4/5, RR Sport, RR, Velar options)",
            cautions = listOf(
                "A car 'stuck low / won't raise' is almost always a genuine fault " +
                    "(sensor, compressor, valve block, leak) — there is no secret " +
                    "button unlock, and battery-disconnect 'resets' just mask it.",
                "'Not in customer mode' on the dash is a technician calibration state — " +
                    "never hand a car over showing it.",
            ),
            procedure = listOf(
                "Suspect transport mode on a fresh trade-in/auction car whose wipers, " +
                    "locks, lights or speedo act dead while the suspension works: don't " +
                    "chase wiring — book the PDI/transport-mode exit on JLR-approved " +
                    "diagnostics (dealers report it can take ~2 hours).",
                "After ANY height-sensor, strut, compressor or valve-block work: " +
                    "calibrate with a proper EAS-capable tool (GAP IIDTool / SDD) — " +
                    "there is no dash route and no dongle route.",
            ),
        ),
    )
}
