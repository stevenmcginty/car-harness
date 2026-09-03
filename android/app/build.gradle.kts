plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.carharness.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.carharness.app"
        // Android 5.0. The floor for Jetpack Compose, and low enough for the
        // Autel MaxiPRO MP808 workshop tablet. Nothing in this app needs 26:
        // the only two API-gated calls (canRequestPackageInstalls, the S
        // Bluetooth permissions) are already behind SDK_INT checks.
        minSdk = 21
        targetSdk = 35
        versionCode = 29
        versionName = "2.19"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation(project(":core"))
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // Relay mode's one WebSocket to the desktop. 4.x is the last line that
    // still supports API 21, which this app's Autel-tablet floor needs.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
