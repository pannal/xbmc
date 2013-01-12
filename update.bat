@ECHO off

echo cleaning
rmdir /Q /S .\project\Win32BuildSetup\BUILD_WIN32
rmdir /Q /S .\project\VS2010Express\XBMC

echo updating
git fetch pannal dsplayer
git fetch origin dsplayer
git pull -s recursive -X theirs origin dsplayer

echo updating
git push pannal HEAD:dsplayer
cd project\Win32BuildSetup

echo building
BuildSetup.bat noprompt
cd ..\..