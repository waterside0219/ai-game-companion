$ErrorActionPreference='Stop'
. "$PSScriptRoot\..\win\desk-overlay.ps1" -SelfTest
# Real Add-Type compilation, but no hook/microphone/network/service starts in SelfTest.
$c=[pscustomobject]@{side_button='XBUTTON1'}
Update-DeskConfig $c
if($c.side_button -ne 'XBUTTON2' -or $c.voice_button -ne 'XBUTTON1' -or $c.bottom_subtitles){throw 'legacy defaults migration'}
$c.side_button='XBUTTON1';$c.voice_button='XBUTTON2';Update-DeskConfig $c
if($c.side_button -ne 'XBUTTON1' -or $c.voice_button -ne 'XBUTTON2'){throw 'custom v5 buttons reset'}
$wav=[DeskRecorder]::Wave([byte[]]::new(32000),32000)
if($wav.Length -ne 32044 -or [Text.Encoding]::ASCII.GetString($wav,0,4) -ne 'RIFF' -or [BitConverter]::ToInt32($wav,24) -ne 16000){throw 'WAV contract'}
[DeskNative]::SetVoiceState($true);[DeskNative]::SetVoiceState($true);[DeskNative]::SetVoiceState($false)
$down=[DeskNative]::TakeVoice();$up=[DeskNative]::TakeVoice()
if(-not $down.down -or $up.down -or $null -ne [DeskNative]::TakeVoice()){throw 'edge deduplication'}
'PASS: native compilation, migration once/custom mapping, PCM16 WAV header, side-key edge deduplication'
