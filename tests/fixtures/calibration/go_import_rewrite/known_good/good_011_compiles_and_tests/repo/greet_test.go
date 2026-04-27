package good011

import "testing"

func TestGreet(t *testing.T) {
	if got := Greet(); got != "hello" {
		t.Fatalf("Greet() = %q, want %q", got, "hello")
	}
}
