package bad013

import "testing"

func TestAnswerWrongExpectation(t *testing.T) {
	// Intentional mismatch: Answer returns 42 but the test asserts 41.
	if got := Answer(); got != 41 {
		t.Fatalf("Answer() = %d, want 41", got)
	}
}
