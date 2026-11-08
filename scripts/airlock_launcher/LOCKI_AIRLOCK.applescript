-- LOCKI AIRLOCK: the one double-click. Starts or reuses the M4 services, restores the M5 tunnels, checks real readiness and opens
-- the owner dashboard. It never arms, never turns the camera or microphone on, and never grants evidence.
on run
	set homePath to POSIX path of (path to home folder)
	set launcher to homePath & ".locki/launcher/locki_launch.sh"
	set hasURL to do shell script "grep -q '^LOCKI_DASHBOARD_URL=' " & quoted form of (homePath & ".locki/launcher.env") & " 2>/dev/null && echo yes || echo no"
	if hasURL is "no" then
		set pasted to text returned of (display dialog "One-time setup: paste your OWNER dashboard address from the owner Chrome window (it starts with http://127.0.0.1:5174/ and contains client=owner-recovered-). It holds no password." default answer "" with title "LOCKI AIRLOCK" buttons {"Cancel", "Save"} default button "Save")
		set saved to do shell script "/usr/bin/python3 -I -c 'import sys,urllib.parse as u,os,shlex; x=sys.argv[1].strip(); p=u.urlsplit(x); q=u.parse_qs(p.query); ok=p.scheme==\"http\" and p.netloc in (\"127.0.0.1:5174\",\"localhost:5174\") and q.get(\"client\",[\"\"])[0].startswith(\"owner-recovered-\"); f=os.path.expanduser(\"~/.locki/launcher.env\"); ok and open(f,\"a\").write(\"LOCKI_DASHBOARD_URL=\"+shlex.quote(x)+chr(10)); print(\"ok\" if ok else \"bad\")' " & quoted form of pasted
		if saved is not "ok" then
			display dialog "That isn't the owner dashboard address (it must be http://127.0.0.1:5174/… with client=owner-recovered-…). Open the owner Chrome window, copy the address bar, and try again." with title "LOCKI AIRLOCK" buttons {"OK"} with icon caution
			return
		end if
	end if
	repeat
		display notification "Checking the M4, Jetson, M5 Nemotron and audio…" with title "LOCKI AIRLOCK" subtitle "Starting"
		set out to do shell script "/bin/bash " & quoted form of launcher & " --app"
		set ls to paragraphs of out
		set state to item 1 of ls
		set secs to item 2 of ls
		set details to ""
		if (count of ls) > 2 then
			repeat with i from 3 to count of ls
				set details to details & item i of ls & return
			end repeat
		end if
		if state is "READY" then
			display notification "Every required check passed in " & secs & " s. Press START PROTECTION when you're ready." with title "LOCKI AIRLOCK" subtitle "READY"
			if details is not "" then
				display dialog "LOCKI AIRLOCK is READY (" & secs & " s)." & return & return & details with title "LOCKI AIRLOCK" buttons {"OK"} default button "OK" giving up after 20
			end if
			exit repeat
		else
			set b to button returned of (display dialog "LOCKI AIRLOCK: " & state & " after " & secs & " s" & return & return & details with title "LOCKI AIRLOCK" buttons {"Close", "Retry"} default button "Retry" with icon caution)
			if b is "Close" then exit repeat
		end if
	end repeat
end run
