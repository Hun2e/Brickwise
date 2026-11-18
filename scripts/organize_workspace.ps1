param(
    [switch]$Apply,
    [switch]$ArchiveUnreadable,
    [ValidateRange(1, 8760)]
    [int]$OlderThanHours = 2
)

# 기본 실행은 미리보기. 삭제 없이 오래된 테스트 폴더를 보관한다.
$ErrorActionPreference = 'Stop'
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$workspacePrefix = $workspaceRoot + [IO.Path]::DirectorySeparatorChar
$cutoff = [DateTime]::UtcNow.AddHours(-$OlderThanHours)
$batch = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
$archiveRoot = Join-Path $workspaceRoot ".tmp-work/archive/$batch"
$records = [Collections.Generic.List[object]]::new()

function Assert-WorkspacePath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the workspace: $absolute"
    }
    return $absolute
}

foreach ($location in @('', 'tests')) {
    $parent = if ($location) { Join-Path $workspaceRoot $location } else { $workspaceRoot }
    $folders = Get-ChildItem -LiteralPath $parent -Force -Directory | Where-Object {
        $_.Name -match '^\.(tmp[-_]|pytest[-_])' -and
        $_.Name -notin @('.tmp-work', '.pytest_cache')
    }
    foreach ($folder in $folders) {
        $relative = if ($location) { "$location/$($folder.Name)" } else { $folder.Name }
        $source = Assert-WorkspacePath $folder.FullName
        $destination = Assert-WorkspacePath (Join-Path $archiveRoot $relative)
        $record = [pscustomobject]@{ source = $source; destination = $destination; status = 'preview'; reason = '' }
        try {
            if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Directory link; preserved.'
            }
            $tracked = @(git -C $workspaceRoot ls-files -- $relative)
            if ($LASTEXITCODE -ne 0) { throw 'Cannot verify Git tracking.' }
            if ($tracked.Count) { throw 'Contains tracked files; preserved.' }
            # 자식 파일의 최근 변경도 확인하여 진행 중인 실행을 보존한다.
            $children = @()
            try {
                $children = @(Get-ChildItem -LiteralPath $source -Force -Recurse -ErrorAction Stop)
            } catch {
                if (-not $ArchiveUnreadable -or
                    $_.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::PermissionDenied) {
                    throw
                }
                # 명시적으로 선택한 경우에만 내부를 읽지 못한 오래된 폴더를 통째로 이름 변경한다.
                # 자식에는 접근하지 않으며 원래 ACL도 유지한다.
                $record.reason = 'Contents unreadable; directory timestamp checked, ACL preserved.'
            }
            if (@($children | Where-Object {
                ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $_.Name -eq '.git'
            }).Count) { throw 'Contains a link or repository; preserved.' }
            if ($folder.LastWriteTimeUtc -gt $cutoff -or @($children | Where-Object {
                $_.LastWriteTimeUtc -gt $cutoff
            }).Count) { throw 'Recently modified; preserved.' }
            if (Test-Path -LiteralPath $destination) { throw 'Destination already exists; preserved.' }
            if ($Apply) {
                $destinationParent = Split-Path -Parent $destination
                New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
                # 같은 볼륨 내 이름 변경이므로 내용을 복사하거나 삭제하지 않는다.
                [IO.Directory]::Move($source, $destination)
                $record.status = 'moved'
            }
        } catch {
            $record.status = 'preserved'
            $record.reason = $_.Exception.Message
        }
        $records.Add($record)
        if ($Apply -and (Test-Path -LiteralPath $archiveRoot)) {
            ConvertTo-Json -InputObject @($records.ToArray()) -Depth 4 |
                Set-Content -LiteralPath (Join-Path $archiveRoot 'manifest.json') -Encoding UTF8
        }
    }
}

$records | Group-Object status | Select-Object Name, Count | Format-Table -AutoSize
if ($Apply -and (Test-Path -LiteralPath $archiveRoot)) {
    Write-Output "Manifest: $(Join-Path $archiveRoot 'manifest.json')"
}
$preserved = @($records | Where-Object status -eq 'preserved')
if ($preserved.Count) {
    Write-Output 'Preserved reasons (details are in manifest.json when applying):'
    $preserved | Group-Object {
        if ($_.reason -match 'denied|거부') { 'Access denied' } else { $_.reason }
    } | Select-Object Name, Count | Format-Table -AutoSize -Wrap
}
