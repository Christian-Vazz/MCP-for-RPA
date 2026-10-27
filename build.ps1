$exclude = @("venv", "botcity.zip")
$files = Get-ChildItem -Path . -Exclude $exclude
Compress-Archive -Path $files -DestinationPath "botcity.zip" -Force