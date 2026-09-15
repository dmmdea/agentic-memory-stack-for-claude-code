//go:build windows

package lock

// claimSingleton uses the named mutex on Windows. The path is unused: a file lock adds
// nothing a desktop-scoped kernel object does not already give, and a leftover file
// would have to be aged out by hand.
func claimSingleton(name, _ string) (func(), bool, error) {
	h, ok, err := acquireMutex(name)
	if err != nil || !ok {
		return nil, false, err
	}
	return func() { releaseMutex(h) }, true, nil
}
