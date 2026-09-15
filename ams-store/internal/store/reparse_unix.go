//go:build !windows

package store

import "io/fs"

// isReparsePoint reports whether a FileInfo is a symbolic link. Junctions do not exist
// off Windows; symlinks do, and they alias a store exactly the same way.
func isReparsePoint(fi fs.FileInfo) bool {
	return fi != nil && fi.Mode()&fs.ModeSymlink != 0
}

// foldCase reports whether store keys are case-folded on this platform.
//
// False off Windows, deliberately. The PowerShell original lower-cases the canonical key
// (LIB:123, :127, :156) because it only ever ran on Windows; on a case-sensitive
// filesystem that folding would collapse two genuinely distinct workspaces differing
// only in case into one and silently mark one of them an alias of the other. Decision
// Q5: gate the folding on the OS and warn when two stores differ only in case.
func foldCase() bool { return false }
