# Regenerate: vhs docs/demo.tape  (needs `govint sync csl`, `govint sync faa` and `govint config contact` first)
Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 700
Set Padding 20
Set Theme "Dracula"
Set TypingSpeed 45ms

Hide
Type "export PS1='$ ' COLUMNS=130 && clear"
Enter
Show

Type "# Is this company on a US sanctions or export list?"
Enter
Type `govint screen "Huawei Technologies"`
Sleep 400ms
Enter
Sleep 3.5s

Type "clear"
Enter
Type "# Who owns this jet?"
Enter
Type "govint tail N628TS"
Sleep 400ms
Enter
Sleep 3.5s

Type "clear"
Enter
Type "# Which insiders are selling stock?"
Enter
Type `govint edgar "Palantir Technologies" --filings 0`
Sleep 400ms
Enter
Sleep 5s

Type "clear"
Enter
Type "# How much federal money has it received?"
Enter
Type `govint spend "Palantir Technologies" --limit 3`
Sleep 400ms
Enter
Sleep 5s
