$Host.UI.RawUI.WindowTitle = "AI-Advent-Day3"
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\moshkin\Projects\ai-advent-10\day03"
Write-Host ""
Write-Host "Day 3. Reasoning modes"
Write-Host "8 tasks, 4 prompt styles, 2 reasoning levels"
Write-Host ""
Start-Sleep -Seconds 3
Remove-Item -Force runs.json -ErrorAction SilentlyContinue
python -u compare_reasoning.py
Write-Host ""
Write-Host "Done. Report: day03/report.md"
Start-Sleep -Seconds 8
