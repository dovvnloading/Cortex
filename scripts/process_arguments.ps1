# Quote arguments for Start-Process on Windows.
#
# Start-Process -ArgumentList joins its array with spaces and quotes nothing,
# so a path such as "C:\Users\Jane Doe\AppData\Local\Temp\x" reaches the
# child as several arguments. Dot-source this file and pass each argument
# through ConvertTo-ProcessArgument first.

function ConvertTo-ProcessArgument([string]$Value) {
    # The quoting the Microsoft C runtime and CommandLineToArgvW undo: inside
    # quotes, backslashes are literal except before a quote, where 2n
    # backslashes plus \" stand for n backslashes and a quote character.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $escaped = [regex]::Replace($Value, '(\\*)"', { param($match) ($match.Groups[1].Value * 2) + '\"' })
    $escaped = [regex]::Replace($escaped, '(\\+)$', { param($match) $match.Groups[1].Value * 2 })
    return '"' + $escaped + '"'
}
