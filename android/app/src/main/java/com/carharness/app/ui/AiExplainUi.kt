package com.carharness.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.ai.AiExplain
import com.carharness.app.ai.AiFaultContext
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * "Explain with AI" under a fault code. Idle -> a quiet link; tap runs one
 * Gemini lookup with the car context and shows the answer inline, with an
 * honest "AI can be wrong" footer. No key saved yet -> a paste-your-key
 * dialog first (free tier, stored only on this phone).
 */
@Composable
fun AiExplainBlock(code: String, rawHex: String?, ctx: AiFaultContext) {
    val appCtx = LocalContext.current
    var answer by remember(code) { mutableStateOf<String?>(null) }
    var error by remember(code) { mutableStateOf<String?>(null) }
    var busy by remember(code) { mutableStateOf(false) }
    var showKeyDialog by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun ask() {
        val key = AiExplain.keyOf(appCtx) ?: run { showKeyDialog = true; return }
        busy = true
        error = null
        scope.launch {
            try {
                answer = withContext(Dispatchers.IO) {
                    AiExplain.explain(key, AiExplain.prompt(code, rawHex, ctx))
                }
            } catch (e: Exception) {
                error = e.message ?: "AI lookup failed."
            } finally {
                busy = false
            }
        }
    }

    Spacer(Modifier.height(6.dp))
    when {
        busy -> Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(Modifier.size(13.dp), strokeWidth = 2.dp, color = Blue)
            Spacer(Modifier.width(8.dp))
            Text("Asking the AI…", color = TextFaint, fontSize = 12.sp)
        }
        answer != null -> Column(
            Modifier
                .fillMaxWidth()
                .background(Bg, RoundedCornerShape(10.dp))
                .border(1.dp, RuleInner, RoundedCornerShape(10.dp))
                .padding(10.dp),
        ) {
            Text(
                "AI EXPLANATION", color = Blue, fontFamily = Barlow, fontSize = 9.sp,
                letterSpacing = 1.4.sp, fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(4.dp))
            Text(answer!!, color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp)
            Spacer(Modifier.height(4.dp))
            Text(
                "AI can be wrong — check before spending money.",
                color = TextFaint, fontSize = 10.sp,
            )
        }
        else -> Column {
            Text(
                "✨ Explain with AI",
                color = Blue, fontSize = 12.sp, fontWeight = FontWeight.Medium,
                modifier = Modifier.clickable { ask() },
            )
            error?.let {
                Spacer(Modifier.height(3.dp))
                Text(it, color = Amber, fontSize = 11.sp, lineHeight = 15.sp)
            }
        }
    }

    if (showKeyDialog) {
        AiKeyDialog(
            onDismiss = { showKeyDialog = false },
            onSave = { k ->
                AiExplain.saveKey(appCtx, k)
                showKeyDialog = false
                ask()
            },
        )
    }
}

@Composable
private fun AiKeyDialog(onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var key by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Panel,
        title = { Text("AI lookups need a key", color = TextMain, fontSize = 17.sp) },
        text = {
            Column {
                Text(
                    "Paste a free Google Gemini API key — get one at aistudio.google.com " +
                        "with a Google account, no card needed. The key stays on this phone " +
                        "only, and each lookup sends just the fault code and car details.",
                    color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(
                    value = key,
                    onValueChange = { key = it },
                    singleLine = true,
                    placeholder = { Text("AIza…", color = TextFaint) },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { if (key.isNotBlank()) onSave(key) }) {
                Text("Save & ask", color = Green, fontWeight = FontWeight.SemiBold)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel", color = TextFaint) }
        },
    )
}
