# VHS tape for the README demo GIF.
# Install vhs: https://github.com/charmbracelet/vhs
# Then run: vhs record.tape

Output demo.gif
Set Shell "bash"
Set FontSize 18
Set Width 900
Set Height 540
Set TypingSpeed 50ms

Type "cd demos/invented-api && python run.py"
Enter
Sleep 1s

# Hold the final frame so the GIF is readable.
Sleep 5s
