package bad013

// Answer returns the actual answer (42). The accompanying test asserts
// 41, so go test fails even though go build succeeds — exactly the
// situation tier-2 exists to catch.
func Answer() int {
	return 42
}
