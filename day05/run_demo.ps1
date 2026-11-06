$Host.UI.RawUI.WindowTitle = "AI-Advent-Day5"
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
Set-Location "C:\Users\moshkin\Projects\ai-advent-10\day05"
Write-Host ""
Write-Host "Day 5. Model versions"
Write-Host "DeepSeek Flash, Gemma 31B free, DeepSeek V4 Pro"
Write-Host ""
Start-Sleep -Seconds 3
Remove-Item -Force runs.json -ErrorAction SilentlyContinue
python -u compare_models.py
Write-Host ""
Write-Host "Done. Report: day05/report.md"
Start-Sleep -Seconds 8
