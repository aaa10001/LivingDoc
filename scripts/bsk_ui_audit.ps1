# bsk_ui_audit.ps1 — real-browser audit of the LivingDoc UI via the bsk CLI.
#
# Requires: bsk daemon running + a connected Chromium with the BrowserSkill extension.
# Produces: screenshots (PNG) + report.json under -OutDir.
#
# Usage:
#   $env:PATH = "<repo>\.bsk-cli;" + $env:PATH
#   $env:BSK_HOME = "<repo>\.bsk-home"
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bsk_ui_audit.ps1
#
# Gotchas baked in:
#  * bsk evaluate takes ONE argument and PowerShell 5.1 mangles embedded double quotes when
#    calling native exes, so every expression below uses single quotes only, with no newlines.
#  * Console entries with url chrome-extension://invalid/ come from the BrowserSkill extension
#    itself, not from the page under test, so they are filtered out.

param(
    [string]$BaseUrl = "http://127.0.0.1:8001",
    [string]$OutDir = "var/ui-audit",
    [int]$Width = 1600,
    [int]$Height = 1200
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

function Invoke-Bsk {
    param([string[]]$BskArgs)
    $out = & bsk @BskArgs 2>$null
    return ($out | Out-String).Trim()
}

$OutDir = Join-Path (Get-Location) $OutDir
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$session = Invoke-Bsk @("session", "start")
if (-not $session) {
    $line = (Invoke-Bsk @("session", "list")) -split "`n" | Where-Object { $_ -match '^\S+\s+\S+\s+\d+$' } | Select-Object -First 1
    if ($line) { $session = ($line -split '\s+')[0] }
}
if (-not $session) { throw "could not start or find a bsk session" }
Write-Host "session=$session"

$null = Invoke-Bsk @("window", "resize", "--session", $session, "--width", "$Width", "--height", "$Height")

$js = @{
    structure = @'
JSON.stringify({url:location.href,title:document.title,h1:[...document.querySelectorAll('h1')].map(e=>e.textContent.trim()),h2:[...document.querySelectorAll('h2')].map(e=>e.textContent.trim()),h3:[...document.querySelectorAll('h3')].map(e=>e.textContent.trim()),sections:document.querySelectorAll('section').length,cards:document.querySelectorAll('.card, .panel, article').length,tables:document.querySelectorAll('table').length,images:document.querySelectorAll('img').length,codeBlocks:document.querySelectorAll('pre, code').length,links:[...document.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')),docHeight:document.documentElement.scrollHeight})
'@
    a11y = @'
JSON.stringify((()=>{const noAlt=[...document.querySelectorAll('img')].filter(i=>!i.hasAttribute('alt')).length;const brokenImgs=[...document.querySelectorAll('img')].filter(i=>i.complete&&i.naturalWidth===0).map(i=>i.currentSrc||i.src);const ids=[...document.querySelectorAll('[id]')].map(e=>e.id);const dupIds=ids.filter((v,i)=>ids.indexOf(v)!==i);const emptyLinks=[...document.querySelectorAll('a[href]')].filter(a=>['','#'].includes(a.getAttribute('href'))).length;const levels=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h=>+h.tagName[1]);const jumps=[];for(let i=1;i<levels.length;i++){if(levels[i]-levels[i-1]>1){jumps.push(levels[i-1]+'->'+levels[i])}}const noLang=!document.documentElement.lang;const inputsWithoutLabel=[...document.querySelectorAll('input, select, textarea')].filter(e=>!(e.labels&&e.labels.length)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')&&!e.placeholder).length;return{noAlt,brokenImgs,dupIds,emptyLinks,headingJumps:jumps,noLang,buttons:document.querySelectorAll('button, [role=button]').length,inputsWithoutLabel}})())
'@
    layout = @'
JSON.stringify((()=>{const de=document.documentElement;const wide=[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&(r.right>de.clientWidth+1||r.left<-1)}).slice(0,8).map(e=>e.tagName.toLowerCase()+(e.className&&typeof e.className==='string'?'.'+e.className.trim().split(/\s+/).join('.'):''));const small=[...document.querySelectorAll('a[href], button, [role=button]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(r.height<32||r.width<32)}).slice(0,8).map(e=>{const r=e.getBoundingClientRect();return (e.textContent||'').trim().slice(0,20)+' ('+Math.round(r.width)+'x'+Math.round(r.height)+')'});const cs=getComputedStyle(document.body);const parse=s=>(s.match(/[\d.]+/g)||[0,0,0]).slice(0,3).map(Number);const lum=c=>{const a=c.map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)});return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2]};const L1=lum(parse(cs.color));const L2=lum(parse(cs.backgroundColor==='rgba(0, 0, 0, 0)'?'rgb(255,255,255)':cs.backgroundColor));const ratio=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);return{viewport:de.clientWidth+'x'+de.clientHeight,horizontalOverflow:de.scrollWidth>de.clientWidth+1,scrollWidth:de.scrollWidth,clientWidth:de.clientWidth,overflowing:wide,smallTargets:small,bodyFont:cs.fontFamily,bodyColor:cs.color,bodyBg:cs.backgroundColor,contrastRatio:Math.round(ratio*100)/100,darkMode:matchMedia('(prefers-color-scheme: dark)').matches}})())
'@
}

$report = [ordered]@{
    generatedAt = (Get-Date).ToString("s")
    baseUrl     = $BaseUrl
    session     = $session
    pages       = @()
}

function Test-Page {
    param([string]$Path, [string]$Name, [string]$Device = "")
    $url = "$BaseUrl$Path"
    $null = Invoke-Bsk @("navigate", "--session", $session, $url)
    Start-Sleep -Seconds 2
    if ($Device) { $null = Invoke-Bsk @("emulate", "--session", $session, "--device", $Device) }
    Start-Sleep -Milliseconds 800

    $entry = [ordered]@{ name = $Name; url = $url; device = $Device }
    foreach ($k in @("structure", "a11y", "layout")) {
        $oneLine = ($js[$k] -replace "\r?\n", " ").Trim()
        $raw = Invoke-Bsk @("evaluate", "--session", $session, $oneLine)
        try { $entry[$k] = ($raw | ConvertFrom-Json) } catch { $entry[$k] = @{ error = $raw } }
    }

    $console = Invoke-Bsk @("console", "--session", $session, "--json", "--limit", "200")
    try {
        $c = $console | ConvertFrom-Json
        $entry.console = @($c.entries |
            Where-Object { $_.level -in @("error", "warning", "warn") -and $_.url -notlike "chrome-extension://*" } |
            ForEach-Object { "$($_.level): $($_.text)" })
    } catch { $entry.console = @("unparsed: $console") }

    $net = Invoke-Bsk @("network", "--session", $session, "--json", "--limit", "200")
    try {
        $n = $net | ConvertFrom-Json
        $entry.networkFailures = @($n.entries | Where-Object { $_.failed -or ($_.status -and $_.status -ge 400) } | ForEach-Object { "$($_.status) $($_.url)" })
    } catch { $entry.networkFailures = @("unparsed: $net") }

    # bsk screenshot only grabs the viewport, so walk the page in viewport-sized slices.
    $stem = "{0}{1}" -f $Name, $(if ($Device) { "-$Device" } else { "" })
    $vp = Invoke-Bsk @("evaluate", "--session", $session, "JSON.stringify({h:document.documentElement.scrollHeight,v:window.innerHeight})")
    try { $dims = $vp | ConvertFrom-Json } catch { $dims = @{ h = 0; v = 1 } }
    $step = [int]$dims.v
    $slices = [math]::Max(1, [math]::Ceiling($dims.h / [math]::Max(1, $step - 40)))
    $shots = @()
    for ($i = 0; $i -lt $slices; $i++) {
        $y = $i * ($step - 40)
        $null = Invoke-Bsk @("evaluate", "--session", $session, "scrollTo(0,$y); scrollY")
        Start-Sleep -Milliseconds 350
        $png = Join-Path $OutDir ("{0}-full-{1}.png" -f $stem, $i)
        $null = Invoke-Bsk @("screenshot", "--session", $session, "--out", $png)
        $shots += $png
    }
    $null = Invoke-Bsk @("evaluate", "--session", $session, "scrollTo(0,0); scrollY")
    $entry.screenshots = $shots
    $entry.docHeight = $dims.h
    $report.pages += $entry
    if ($Device) { $null = Invoke-Bsk @("emulate", "--session", $session, "--off") }
    return $entry
}

$null = Test-Page -Path "/ui/showcase.html" -Name "showcase-desktop"
$null = Test-Page -Path "/ui/showcase.html" -Name "showcase-mobile" -Device "iphone-14"
$null = Test-Page -Path "/ui/" -Name "review-desktop"

$jsonPath = Join-Path $OutDir "report.json"
$report | ConvertTo-Json -Depth 8 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Host "report=$jsonPath"