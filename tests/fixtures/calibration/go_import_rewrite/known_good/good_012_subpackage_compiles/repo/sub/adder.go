package sub

// Add returns x + y. Trivial subpackage so the calibration corpus has
// an intra-module import to exercise.
func Add(x, y int) int {
	return x + y
}
