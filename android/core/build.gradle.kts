plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.carharness.core"
    compileSdk = 35

    defaultConfig {
        // Android 4.4 (KitKat). This is the floor for the Autel MaxiPRO MP808
        // workshop tablet, and it costs nothing: the engine uses no AndroidX
        // and no Compose — only the framework, the Java stdlib and org.json.
        minSdk = 19
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // coroutines-core, NOT coroutines-android: the pure-JVM artifact carries no
    // minSdk floor of its own. The Main dispatcher is a UI concern, so each app
    // module brings coroutines-android itself.
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.9.0")
}
