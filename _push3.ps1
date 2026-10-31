$ErrorActionPreference = 'Continue'
$cred = (Get-Content "$env:USERPROFILE\.git-credentials" -Raw).Trim()
if ($cred -notmatch 'https://([^:]+):([^@]+)@github\.com') { Write-Output 'bad cred'; exit 1 }
$url = "https://$($Matches[1])`:$($Matches[2])@github.com/punchinlord-bit/btc-edge.git"

Set-Location C:\Users\Admin\Desktop\BTC
$env:GIT_TERMINAL_PROMPT = '0'
git config http.sslBackend schannel

# Clean temp logs
Remove-Item -LiteralPath v_pt.log,v_pt_err.log,v_ruff.log,v_ruff_err.log,v_mypy.log,v_mypy_err.log -Force -ErrorAction SilentlyContinue

git add -A
git -c user.name='punchinlord-bit' -c user.email='punchinlord@icloud.com' commit -m 'Fix DeepSeek wiring: read AI_BASE_URL/AI_MODEL from env; untrack runtime logs' 2>&1 | Out-File c.txt -Encoding utf8
git push "$url" main:main 2>&1 | Out-File p.txt -Encoding utf8
Write-Output '--- commit ---'; Get-Content c.txt -Tail 4
Write-Output '--- push ---'; Get-Content p.txt -Tail 12
