$Host.UI.RawUI.WindowTitle = "AI-Advent-Day4"
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\moshkin\Projects\ai-advent-10\day04"
Write-Host ""
Write-Host "Day 4. Temperature"
Write-Host "temperature 0, 0.7 and 1.2, two tasks, 5 runs each"
Write-Host ""
Start-Sleep -Seconds 3
python -u compare_temperature.py
Write-Host ""
Write-Host "Done. Report: day04/report.md"
Start-Sleep -Seconds 8
