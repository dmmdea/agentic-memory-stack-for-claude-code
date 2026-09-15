//go:build windows

package store

import (
	"io/fs"
	"syscall"
)

// isReparsePoint reports whether a FileInfo carries FILE_ATTRIBUTE_REPARSE_POINT.
//
// The raw attribute bit is the test, not fs.ModeSymlink: a directory JUNCTION - the
// shape actually observed aliasing a workspace, spaces-vs-dashes - is a reparse point
// that Go does not always surface as a symlink mode bit. The PowerShell original tests
// the same bit (LIB:118).
func isReparsePoint(fi fs.FileInfo) bool {
	if fi == nil {
		return false
	}
	if d, ok := fi.Sys().(*syscall.Win32FileAttributeData); ok {
		return d.FileAttributes&syscall.FILE_ATTRIBUTE_REPARSE_POINT != 0
	}
	return fi.Mode()&fs.ModeSymlink != 0
}

// foldCase reports whether store keys are case-folded on this platform. On Windows the
// filesystem folds, so the key must too.
func foldCase() bool { return true }
