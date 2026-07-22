package com.carharness.app.update

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

data class ReleaseInfo(
    val version: String,
    val tag: String,
    val title: String,
    val apkUrl: String,
    val apkBytes: Long,
    val newer: Boolean,
)

/**
 * In-app updates from GitHub Releases. Checks the latest release of the
 * public repo, downloads the APK asset, and hands it to the system package
 * installer. Works because every release APK is signed with the same key.
 */
object AppUpdater {
    private const val REPO = "stevenmcginty/car-harness"

    fun installedVersion(context: Context): String = try {
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "?"
    } catch (_: Exception) {
        "?"
    }

    /** GET the latest release from the GitHub API and compare versions. */
    fun checkLatest(context: Context): ReleaseInfo {
        val conn = URL("https://api.github.com/repos/$REPO/releases/latest")
            .openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 15000
        conn.setRequestProperty("Accept", "application/vnd.github+json")
        conn.setRequestProperty("User-Agent", "CarHarness-Android")
        val text = try {
            val code = conn.responseCode
            if (code == 404) throw IOException("No releases published yet.")
            if (code == 403) throw IOException("GitHub is rate-limiting this network — try again in a while.")
            if (code !in 200..299) throw IOException("GitHub returned HTTP $code.")
            conn.inputStream.bufferedReader().use { it.readText() }
        } finally {
            conn.disconnect()
        }

        val json = JSONObject(text)
        val tag = json.optString("tag_name")
        if (tag.isEmpty()) throw IOException("Couldn't read the release info.")
        val version = tag.removePrefix("v").removePrefix("V")
        val assets = json.optJSONArray("assets")
        var apkUrl = ""
        var apkBytes = 0L
        if (assets != null) {
            for (i in 0 until assets.length()) {
                val asset = assets.getJSONObject(i)
                if (asset.optString("name").endsWith(".apk", ignoreCase = true)) {
                    apkUrl = asset.optString("browser_download_url")
                    apkBytes = asset.optLong("size")
                    break
                }
            }
        }
        if (apkUrl.isEmpty()) throw IOException("Latest release ($tag) has no APK attached.")
        return ReleaseInfo(
            version = version,
            tag = tag,
            title = json.optString("name", tag),
            apkUrl = apkUrl,
            apkBytes = apkBytes,
            newer = isNewer(version, installedVersion(context)),
        )
    }

    /** True when [remote] is a strictly newer dotted version than [local]. */
    fun isNewer(remote: String, local: String): Boolean {
        val r = remote.split(".").map { it.trim().toIntOrNull() ?: 0 }
        val l = local.split(".").map { it.trim().toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(r.size, l.size)) {
            val rv = r.getOrElse(i) { 0 }
            val lv = l.getOrElse(i) { 0 }
            if (rv != lv) return rv > lv
        }
        return false
    }

    /**
     * Download the release APK into the app cache. Reports progress as 0–100,
     * or -1 while the total size is unknown. Returns the downloaded file.
     */
    fun download(context: Context, info: ReleaseInfo, onProgress: (Int) -> Unit): File {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val dest = File(dir, "CarHarness-${info.tag}.apk")
        val conn = URL(info.apkUrl).openConnection() as HttpURLConnection
        conn.connectTimeout = 8000
        conn.readTimeout = 60000
        conn.setRequestProperty("User-Agent", "CarHarness-Android")
        try {
            if (conn.responseCode !in 200..299) {
                throw IOException("Download failed: HTTP ${conn.responseCode}.")
            }
            val total = if (conn.contentLengthLong > 0) conn.contentLengthLong else info.apkBytes
            var done = 0L
            conn.inputStream.use { input ->
                dest.outputStream().use { output ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        done += n
                        onProgress(if (total > 0) ((done * 100) / total).toInt() else -1)
                    }
                }
            }
            if (dest.length() == 0L) throw IOException("Downloaded file is empty.")
            return dest
        } finally {
            conn.disconnect()
        }
    }

    /**
     * Hand the APK to the system installer. Returns false when Android hasn't
     * yet allowed this app to install updates — in that case the settings
     * screen for that toggle is opened, and the caller should ask the user to
     * come back and tap install again.
     */
    fun install(context: Context, apk: File): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
            !context.packageManager.canRequestPackageInstalls()
        ) {
            context.startActivity(
                Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${context.packageName}"),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            return false
        }
        val uri = FileProvider.getUriForFile(
            context, "${context.packageName}.fileprovider", apk
        )
        context.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        )
        return true
    }
}
