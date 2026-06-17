$urls = @(
    "https://www.python.org/",
    "https://cloudboxgifts.com",
    "https://caratx.com",
    "https://bibiandkim.com",
    "https://biron-gems.com/"
)
foreach ($u in $urls) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri $u -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
        $sw.Stop()
        Write-Host ("OK   {0,5}ms  {1}  ({2} bytes)" -f $sw.ElapsedMilliseconds, $u, $r.RawContentLength)
    } catch {
        $sw.Stop()
        Write-Host ("FAIL {0,5}ms  {1}  {2}" -f $sw.ElapsedMilliseconds, $u, $_.Exception.Message)
    }
}
