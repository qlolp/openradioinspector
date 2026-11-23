import groovy.json.JsonOutput
import org.gradle.api.artifacts.component.ModuleComponentIdentifier
import org.gradle.api.artifacts.type.ArtifactTypeDefinition

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

val generatedJni = layout.buildDirectory.dir("generated/jniLibs")
val policyAssets = layout.buildDirectory.dir("generated/fieldPolicyAssets")
val noticeAssets = layout.buildDirectory.dir("generated/fieldNoticeAssets")
val copyPrivacyPolicy by tasks.registering(Sync::class) {
    from(rootProject.file("../../docs/field/privacy.md"))
    into(policyAssets)
}
android {
    namespace = "org.openradioinspector.field"
    compileSdk = 36
    buildToolsVersion = "36.0.0"
    ndkVersion = "28.2.13676358"

    defaultConfig {
        applicationId = "org.openradioinspector.field"
        minSdk = 31
        targetSdk = 36
        val fieldCode = providers.gradleProperty("fieldVersionCode").orNull?.toInt() ?: 1
        require(fieldCode in 1..2100000000) { "Invalid Play versionCode" }
        versionCode = fieldCode
        versionName = providers.gradleProperty("fieldVersionName").get()
        ndk { abiFilters += "arm64-v8a" }
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    sourceSets["main"].jniLibs.srcDir(generatedJni)
    sourceSets["main"].assets.srcDir(policyAssets)
    sourceSets["main"].assets.srcDir(noticeAssets)
    packaging.jniLibs.useLegacyPackaging = false
}

tasks.withType<Sync>().matching { it.name == "processDebugAndroidTestJavaRes" }.configureEach {
    from(rootProject.file("../../contracts/fixtures")) {
        include("field-archive-v1.orifield", "field-android-export-v1.orifield")
    }
}

val workspaceRoot = rootProject.file("../..")
// Request the original archives AGP uses for the AAB's dependencies.pb.
val runtimeArtifacts = providers.provider {
    configurations.getByName("releaseRuntimeClasspath").incoming.artifactView {
        attributes.attribute(ArtifactTypeDefinition.ARTIFACT_TYPE_ATTRIBUTE, "android-aar-or-jar")
    }.artifacts
}
val collectFieldNotices by tasks.registering(Exec::class) {
    description = "Collect notices from the locked Field binary's Rust and Android runtime inputs"
    workingDir = workspaceRoot
    val resolved = runtimeArtifacts.map { collection ->
        collection.artifacts.map { artifact ->
            val id = artifact.id.componentIdentifier
            require(id is ModuleComponentIdentifier) { "Unreviewed runtime component: ${artifact.id.displayName}" }
            mapOf("coordinate" to "${id.group}:${id.module}:${id.version}", "artifact" to artifact.id.displayName,
                "variant" to artifact.variant.displayName, "file" to artifact.file.absolutePath)
        }.sortedWith(compareBy({ it["coordinate"] }, { it["variant"] }, { it["artifact"] }))
    }
    inputs.files(runtimeArtifacts.map { it.artifactFiles })
    inputs.property("resolvedRuntimeArtifacts", resolved)
    inputs.files(fileTree(workspaceRoot) {
        include("LICENSE", "Cargo.lock", "Cargo.toml", "crates/**/Cargo.toml", "rust-toolchain.toml",
            "clients/android/notices/**", "tools/release/native_notices.py", "tools/field/android_notices.py")
    })
    outputs.dir(noticeAssets)
    doFirst {
        val inventory = temporaryDir.resolve("resolved.json")
        inventory.writeText(JsonOutput.toJson(resolved.get()))
        val python = if (System.getProperty("os.name").startsWith("Windows")) "python" else "python3"
        commandLine(python, "-m", "tools.field.android_notices", "collect", "--resolved", inventory,
            "--assets", noticeAssets.get().asFile)
        if (gradle.startParameter.isOffline) args("--offline")
    }
}
val cargoTarget = layout.buildDirectory.dir("cargo")
val androidNdk = androidComponents.sdkComponents.ndkDirectory
val nativeField by tasks.registering(Exec::class) {
    description = "Cross-compile the locked field library using the Android NDK"
    workingDir = workspaceRoot
    inputs.files(fileTree(workspaceRoot) {
        include("Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "crates/**/Cargo.toml",
            "crates/**/*.rs", "crates/**/*.sql", "contracts/**")
    })
    inputs.property("ndkVersion", android.ndkVersion.orEmpty())
    outputs.file(generatedJni.map { it.file("arm64-v8a/libori_android_field.so") })
    val target = "aarch64-linux-android"
    commandLine("cargo", "build", "--locked", "--release", "--target", target, "-p", "ori-android-field")
    doFirst {
        val osName = System.getProperty("os.name")
        val windows = osName.startsWith("Windows")
        val hostTag = when {
            windows -> "windows-x86_64"
            osName == "Mac OS X" -> "darwin-x86_64"
            else -> "linux-x86_64"
        }
        val bin = androidNdk.get().asFile.resolve("toolchains/llvm/prebuilt/$hostTag/bin")
        val cc = bin.resolve("aarch64-linux-android31-clang" + if (windows) ".cmd" else "")
        val cxx = bin.resolve("aarch64-linux-android31-clang++" + if (windows) ".cmd" else "")
        val ar = bin.resolve("llvm-ar" + if (windows) ".exe" else "")
        environment("CC_aarch64_linux_android", cc.absolutePath)
        environment("CXX_aarch64_linux_android", cxx.absolutePath)
        environment("AR_aarch64_linux_android", ar.absolutePath)
        environment("CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER", cc.absolutePath)
        environment("CARGO_TARGET_AARCH64_LINUX_ANDROID_RUSTFLAGS", "-C link-arg=-Wl,-z,max-page-size=16384")
        environment("CARGO_TARGET_DIR", cargoTarget.get().asFile.absolutePath)
        if (gradle.startParameter.isOffline) environment("CARGO_NET_OFFLINE", "true")
    }
    doLast {
        copy {
            from(cargoTarget.get().file("$target/release/libori_android_field.so"))
            into(generatedJni.get().dir("arm64-v8a"))
        }
    }
}
tasks.named("preBuild") { dependsOn(nativeField, copyPrivacyPolicy, collectFieldNotices) }

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.05.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.github.mik3y:usb-serial-for-android:3.9.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20250517")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
}
