package bad014

import "testing"

func TestStoreSetPanics(t *testing.T) {
	// Set will panic with "assignment to entry in nil map" because
	// NewStore leaves values as nil — go test reports the panic as a
	// failure even though compilation succeeded.
	NewStore().Set("a", 1)
}
