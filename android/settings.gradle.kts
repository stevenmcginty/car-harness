pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "CarHarness"
include(":app")
// The 8-brand protocol engine, shared by every front end (Compose phone app
// today, the KitKat Autel tablet app next).
include(":core")
// Autel Car Harness — the workshop-tablet front end. Android 4.4, landscape,
// framework views only. Same engine, different skin.
include(":autel")
