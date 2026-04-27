package bad014

// Store is a deliberately under-initialized container — it returns a
// nil map so the accompanying test triggers a runtime panic. Compiles
// cleanly; tier-2 catches the failure.
type Store struct {
	values map[string]int
}

func NewStore() *Store {
	return &Store{}
}

func (s *Store) Set(key string, value int) {
	s.values[key] = value
}
