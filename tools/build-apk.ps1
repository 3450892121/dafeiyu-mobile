param(
    [string]$JdkHome,
    [string]$SdkHome,
    [switch]$TestOnly
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    # AAPT on Windows does not reliably accept non-ASCII absolute argv paths.
    # Keep the workspace in place and pass ASCII relative build paths instead.
    [string[]]$commandArguments = @(foreach ($argument in $Arguments) {
        if ((Get-Location).ProviderPath -eq $projectRoot -and
            $argument.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            $argument.Substring($projectRoot.Length + 1).Replace('\', '/')
        } else { $argument }
    })
    & $Executable @commandArguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}

function Project-Output {
    param([string]$Relative)
    $target = [IO.Path]::GetFullPath((Join-Path $projectRoot $Relative))
    if (-not $target.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build output must stay inside this project.'
    }
    return $target
}

if (-not $JdkHome) {
    $localJdkRoot = Join-Path $projectRoot '.tools\jdk'
    if (Test-Path -LiteralPath $localJdkRoot) {
        $localJava = Get-ChildItem -LiteralPath $localJdkRoot -Filter java.exe -Recurse -File | Select-Object -First 1
        if ($localJava) { $JdkHome = Split-Path -Parent (Split-Path -Parent $localJava.FullName) }
    }
    if (-not $JdkHome) { $JdkHome = $env:JAVA_HOME }
}
if (-not $JdkHome -or -not (Test-Path -LiteralPath (Join-Path $JdkHome 'bin\javac.exe'))) {
    throw 'JDK 17 is required. Put it in .tools\jdk or pass -JdkHome. No system settings are changed.'
}
$java = Join-Path $JdkHome 'bin\java.exe'
$javac = Join-Path $JdkHome 'bin\javac.exe'
$jar = Join-Path $JdkHome 'bin\jar.exe'
$keytool = Join-Path $JdkHome 'bin\keytool.exe'
$runRoot = Project-Output ('build\run-' + [Guid]::NewGuid().ToString('N'))
$testClasses = Join-Path $runRoot 'test-classes'
New-Item -ItemType Directory -Path $testClasses -Force | Out-Null
Invoke-Checked $javac @('-encoding','UTF-8','--release','8','-d',$testClasses,
    (Join-Path $projectRoot 'app\src\main\java\app\dafeiyu\mobile\Readiness.java'),
    (Join-Path $projectRoot 'app\src\main\java\app\dafeiyu\mobile\SafeTar.java'),
    (Join-Path $projectRoot 'tests\ReadinessTest.java'),
    (Join-Path $projectRoot 'tests\SafeTarTest.java'),
    (Join-Path $projectRoot 'tools\NormalizeZip.java'),
    (Join-Path $projectRoot 'tools\VerifyApk.java'))
Invoke-Checked $java @('-cp',$testClasses,'app.dafeiyu.mobile.ReadinessTest')
Invoke-Checked $java @('-cp',$testClasses,'app.dafeiyu.mobile.SafeTarTest',$runRoot)
if ($TestOnly) { return }

if (-not $SdkHome) { $SdkHome = Join-Path $projectRoot '.tools\android-sdk' }
$buildTools = Join-Path $SdkHome 'build-tools\35.0.0'
$androidJar = Join-Path $SdkHome 'platforms\android-35\android.jar'
$aapt2 = Join-Path $buildTools 'aapt2.exe'
$aapt = Join-Path $buildTools 'aapt.exe'
$zipalign = Join-Path $buildTools 'zipalign.exe'
$d8 = Join-Path $buildTools 'lib\d8.jar'
$apksigner = Join-Path $buildTools 'lib\apksigner.jar'
foreach ($tool in @($androidJar,$aapt2,$aapt,$zipalign,$d8,$apksigner)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "Missing Android SDK file: $tool" }
}

Push-Location -LiteralPath $projectRoot
try {
    Invoke-Checked 'node' @('tools\check-project.mjs')
    Invoke-Checked 'node' @('tools\prepare-assets.mjs')
    $version = ConvertFrom-StringData (Get-Content -LiteralPath 'version.properties' -Raw)
    $runtimeLayout = Get-Content -LiteralPath 'runtime\layout.json' -Raw | ConvertFrom-Json
    $generatedJava = Join-Path $runRoot 'generated-java'
    $classes = Join-Path $runRoot 'classes'
    $dex = Join-Path $runRoot 'dex'
    New-Item -ItemType Directory -Path $generatedJava,$classes,$dex -Force | Out-Null

    # The source manifest uses Gradle namespace; the direct SDK build supplies it here.
    $manifestText = Get-Content -LiteralPath 'app\src\main\AndroidManifest.xml' -Raw
    $manifestText = $manifestText.Replace('<manifest xmlns:', '<manifest package="app.dafeiyu.mobile" xmlns:')
    $manifestFile = Join-Path $runRoot 'AndroidManifest.xml'
    [IO.File]::WriteAllText($manifestFile, $manifestText, [Text.UTF8Encoding]::new($false))
    $compiled = Join-Path $runRoot 'compiled-resources.zip'
    $resourcesApk = Join-Path $runRoot 'resources.ap_'
    # Keep the bounded AAPT/normalization pass for small UI and business assets.
    # The verified large archives are added later as stored ZIP entries, streaming from disk.
    $generatedAssets = Project-Output 'app\build\generated\assets'
    $aaptAssets = Join-Path $runRoot 'aapt-assets'
    New-Item -ItemType Directory -Path $aaptAssets -Force | Out-Null
    foreach ($asset in (Get-ChildItem -LiteralPath $generatedAssets -Recurse -File)) {
        $relativeAsset = $asset.FullName.Substring($generatedAssets.Length + 1).Replace('\', '/')
        if ($relativeAsset.StartsWith('runtime/offline/', [StringComparison]::Ordinal)) { continue }
        $smallAsset = Join-Path $aaptAssets $relativeAsset
        New-Item -ItemType Directory -Path (Split-Path -Parent $smallAsset) -Force | Out-Null
        Copy-Item -LiteralPath $asset.FullName -Destination $smallAsset
    }
    Invoke-Checked $aapt2 @('compile','--dir',(Join-Path $projectRoot 'app\src\main\res'),'-o',$compiled)
    Invoke-Checked $aapt2 @('link','-o',$resourcesApk,'-I',$androidJar,'--manifest',$manifestFile,
        '--java',$generatedJava,'--min-sdk-version',([string]$runtimeLayout.minSdk),'--target-sdk-version',([string]$runtimeLayout.targetSdk),
        '--version-code',$version.versionCode,'--version-name',$version.versionName,
        '-A',$aaptAssets,$compiled)
    $normalizedResources = Join-Path $runRoot 'resources-normalized.ap_'
    Invoke-Checked $java @('-cp',$testClasses,'NormalizeZip',$resourcesApk,$normalizedResources)
    $resourcesApk = $normalizedResources

    $javaSources = @(
        Get-ChildItem -LiteralPath (Join-Path $projectRoot 'app\src\main\java') -Filter '*.java' -Recurse -File
        Get-ChildItem -LiteralPath $generatedJava -Filter '*.java' -Recurse -File
    ) | ForEach-Object { $_.FullName }
    Invoke-Checked $javac (@('-encoding','UTF-8','--release','8','-classpath',$androidJar,'-d',$classes) + $javaSources)
    $classesJar = Join-Path $runRoot 'classes.jar'
    Invoke-Checked $jar @('--create','--file',$classesJar,'-C',$classes,'.')
    Invoke-Checked $java @('-cp',$d8,'com.android.tools.r8.D8','--release','--lib',$androidJar,
        '--min-api','26','--output',$dex,$classesJar)

    $unsigned = Join-Path $runRoot 'unsigned.apk'
    $aligned = Join-Path $runRoot 'aligned.apk'
    Copy-Item -LiteralPath $resourcesApk -Destination $unsigned
    Invoke-Checked $jar @('--update','--file',$unsigned,'-C',$dex,'classes.dex')
    Invoke-Checked $jar @('--update','--no-compress','--file',$unsigned,
        '-C',(Join-Path $projectRoot 'app\build\generated'),'assets/runtime/offline')
    $nativeRoot = Project-Output 'app\build\generated\jni'
    foreach($name in @('libproot.so','libproot-loader.so','libtalloc.so','libandroid-shmem.so')) {
        if(-not(Test-Path -LiteralPath (Join-Path $nativeRoot ('arm64-v8a\'+$name)))){throw "Missing native runtime: $name"}
    }
    $packageLib = Join-Path $runRoot 'native\lib\arm64-v8a'
    New-Item -ItemType Directory -Path $packageLib -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $nativeRoot 'arm64-v8a') -File | Copy-Item -Destination $packageLib
    Invoke-Checked $jar @('--update','--no-compress','--file',$unsigned,'-C',(Join-Path $runRoot 'native'),'lib')
    Invoke-Checked $zipalign @('-P','16','-f','4',$unsigned,$aligned)

    # Local development signing only; never reads another project's or production keys.
    $signingRoot = Project-Output '.local\signing'
    New-Item -ItemType Directory -Path $signingRoot -Force | Out-Null
    $signingFile = Join-Path $signingRoot 'development.jks'
    if (-not (Test-Path -LiteralPath $signingFile)) {
        Invoke-Checked $keytool @('-genkeypair','-keystore',$signingFile,'-alias','development',
            '-storepass','android','-keypass','android','-keyalg','RSA','-keysize','2048',
            '-validity','10000','-dname','CN=Dafeiyu Mobile Development','-noprompt')
    }
    $dist = Project-Output 'dist'
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    $apk = Join-Path $dist ('dafeiyu-mobile-' + $version.versionName + '-arm64.apk')
    Invoke-Checked $java @('-jar',$apksigner,'sign','--ks',$signingFile,'--ks-key-alias','development',
        '--ks-pass','pass:android','--key-pass','pass:android','--out',$apk,$aligned)
    Invoke-Checked $java @('-jar',$apksigner,'verify','--verbose',$apk)
    Invoke-Checked $zipalign @('-c','-P','16','4',$apk)
    Invoke-Checked $aapt @('dump','badging',$apk)
    Invoke-Checked $java @('-cp',$testClasses,'VerifyApk',$apk,
        (Join-Path $projectRoot 'app\build\generated\assets\runtime\payload-manifest.json'),
        (Join-Path $projectRoot 'app\build\generated\assets\runtime\offline\manifest.json'))
    Invoke-Checked 'node' @('tools\fetch-native-sources.mjs','--check')
    $sourceStage = & node 'tools\package-native-sources.mjs'
    if($LASTEXITCODE -ne 0){throw 'Native source packaging failed.'}
    $sourceArchive = Join-Path $dist ('dafeiyu-mobile-' + $version.versionName + '-native-sources.zip')
    Invoke-Checked $jar @('--create','--file',$sourceArchive,'-C',([string]$sourceStage),'.')
    Get-FileHash -LiteralPath $apk -Algorithm SHA256 | Format-List
    Write-Output "APK: $apk"
} finally {
    Pop-Location
}
