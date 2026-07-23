package com.carharness.app.ai

import android.content.Context
import com.carharness.app.obd.VehicleIdentity
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * What the AI is told alongside a fault code, so it can answer for THIS car
 * rather than generically: identity from the VIN, which module reported it,
 * and the sibling codes read in the same breath (four coil codes at once is a
 * battery story; one alone is a coil story — the AI only sees that pattern if
 * we hand it over).
 */
data class AiFaultContext(
    val vehicle: VehicleIdentity? = null,
    val brand: String? = null,            // selected brand label when the VIN didn't decode
    val module: String? = null,           // e.g. "PCM — Engine / powertrain control"
    val siblings: List<String> = emptyList(),
)

/**
 * Fault-code lookup via Google Gemini, for the codes the offline table can't
 * cover — manufacturer-specific ones above all. Bring-your-own API key
 * (free tier at aistudio.google.com), stored ONLY in this phone's app prefs —
 * never bundled, never committed; the public repo stays key-free. Each lookup
 * sends just the code + car context, nothing else.
 */
object AiExplain {
    private const val MODEL = "gemini-flash-lite-latest"

    fun keyOf(context: Context): String? =
        context.getSharedPreferences("ai", Context.MODE_PRIVATE)
            .getString("gemini_key", null)?.trim()?.takeIf { it.isNotEmpty() }

    fun saveKey(context: Context, key: String) {
        context.getSharedPreferences("ai", Context.MODE_PRIVATE)
            .edit().putString("gemini_key", key.trim()).apply()
    }

    fun prompt(code: String, rawHex: String?, ctx: AiFaultContext): String = buildString {
        append("You are helping a UK used-car dealer understand a fault code read over OBD. ")
        val v = ctx.vehicle
        val car = listOfNotNull(v?.year?.toString(), v?.make ?: ctx.brand).joinToString(" ")
        if (car.isNotBlank()) append("Car: $car. ")
        v?.vin?.let { append("VIN $it. ") }
        ctx.module?.let { append("Module: $it. ") }
        append("Fault code: $code")
        if (!rawHex.isNullOrBlank()) append(" (raw $rawHex)")
        append(". ")
        val others = ctx.siblings.filter { it != code }
        if (others.isNotEmpty()) append("Codes read at the same time: ${others.joinToString(", ")}. ")
        append(
            "Explain plainly: what the code means on this make and model if you know it, otherwise " +
                "the generic meaning; the most likely causes, especially any single common cause " +
                "that would set these codes together; how serious it is to drive with; and the " +
                "cheapest sensible checks first. Under 160 words, plain text, no headings."
        )
    }

    /** Blocking network call — run it off the main thread. Throws IOException with an honest message. */
    fun explain(apiKey: String, promptText: String): String {
        val conn = URL(
            "https://generativelanguage.googleapis.com/v1beta/models/$MODEL:generateContent?key=$apiKey"
        ).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 10000
            conn.readTimeout = 30000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject().put(
                "contents",
                JSONArray().put(
                    JSONObject().put("parts", JSONArray().put(JSONObject().put("text", promptText)))
                ),
            )
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            when (val status = conn.responseCode) {
                in 200..299 -> Unit
                400, 403 -> throw IOException("Google rejected the API key — check it and try again.")
                429 -> throw IOException("The free AI quota is used up right now — try again in a minute.")
                else -> throw IOException("AI service returned HTTP $status.")
            }
            val text = conn.inputStream.bufferedReader().use { it.readText() }
            val parts = JSONObject(text)
                .optJSONArray("candidates")?.optJSONObject(0)
                ?.optJSONObject("content")?.optJSONArray("parts")
            val answer = buildString {
                if (parts != null) {
                    for (i in 0 until parts.length()) {
                        append(parts.optJSONObject(i)?.optString("text").orEmpty())
                    }
                }
            }.trim()
            if (answer.isEmpty()) throw IOException("The AI answered with nothing usable.")
            return answer
        } finally {
            conn.disconnect()
        }
    }
}
