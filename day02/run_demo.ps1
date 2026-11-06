$Host.UI.RawUI.WindowTitle = "AI-Advent-Day2"
try { $Host.UI.RawUI.WindowSize = New-Object System.Management.Automation.Host.Size(120, 40) } catch {}
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\moshkin\Projects\ai-advent-10\day02"
Write-Host ""
Write-Host "Day 2. Response format"
Write-Host "Same question, six ways to control the answer."
Write-Host ""
Start-Sleep -Seconds 4
python -u compare_formats.py
Write-Host ""
Write-Host "Done. Report: day02/report.md"
Start-Sleep -Seconds 8
