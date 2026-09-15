package atomic

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// ReadJSONFile decodes a state file into v. It reports found=false when the file does
// not exist, and returns an error when the file exists but is empty or does not parse.
//
// "Absent" and "corrupt" must never collapse into the same answer. An EMPTY file is
// present-but-unparseable: a truncated seal file read as "no seals" re-arms the model on
// every already-shortened line, which is exactly the drift the seal prevents.
func ReadJSONFile(path string, v any) (found bool, err error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read state file %s: %w", path, err)
	}
	if strings.TrimSpace(string(b)) == "" {
		return true, fmt.Errorf("state file is empty (truncated write?): %s", path)
	}
	if err := json.Unmarshal(b, v); err != nil {
		return true, fmt.Errorf("parse state file %s: %w", path, err)
	}
	return true, nil
}

// WriteJSONFile writes v as indented JSON through Write, so a state file gets the same
// temp-and-swap treatment as a store file.
func WriteJSONFile(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("encode state file %s: %w", path, err)
	}
	return WriteBytes(path, append(b, '\n'))
}
