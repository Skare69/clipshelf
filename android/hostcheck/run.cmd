@echo off
rem Host-runnable outbox invariant check (plain JDK, no Android SDK needed).
setlocal
set HERE=%~dp0
set OUT=%TEMP%\clipshelf-hostcheck
if not exist "%OUT%" mkdir "%OUT%"
javac -encoding UTF-8 -d "%OUT%" "%HERE%OutboxPolicyCheck.java" "%HERE%..\app\src\main\java\dev\clipshelf\app\outbox\OutboxPolicy.java" || exit /b 1
java -cp "%OUT%" OutboxPolicyCheck
