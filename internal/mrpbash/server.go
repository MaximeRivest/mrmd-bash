package mrpbash

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os/exec"
	"strings"
	"time"
)

type Server struct {
	cwd         string
	logLevel    string
	logger      *log.Logger
	runtime     *RuntimeManager
	bashVersion string
	bashPath    string
}

func NewServer(cwd, logLevel string, logger *log.Logger) *Server {
	bashPath := "/bin/bash"
	if resolved, err := exec.LookPath("bash"); err == nil {
		bashPath = resolved
	}

	return &Server{
		cwd:         cwd,
		logLevel:    logLevel,
		logger:      logger,
		runtime:     NewRuntimeManager(cwd),
		bashVersion: detectBashVersion(),
		bashPath:    bashPath,
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/mrp/v1/capabilities", s.handleCapabilities)
	mux.HandleFunc("/mrp/v1/reset", s.handleReset)
	mux.HandleFunc("/mrp/v1/execute", s.handleExecute)
	mux.HandleFunc("/mrp/v1/execute/stream", s.handleExecuteStream)
	mux.HandleFunc("/mrp/v1/input", s.handleInput)
	mux.HandleFunc("/mrp/v1/input/cancel", s.handleInputCancel)
	mux.HandleFunc("/mrp/v1/interrupt", s.handleInterrupt)
	mux.HandleFunc("/mrp/v1/complete", s.handleComplete)
	mux.HandleFunc("/mrp/v1/inspect", s.handleInspect)
	mux.HandleFunc("/mrp/v1/hover", s.handleHover)
	mux.HandleFunc("/mrp/v1/variables", s.handleVariables)
	mux.HandleFunc("/mrp/v1/variables/", s.handleVariableDetail)
	mux.HandleFunc("/mrp/v1/is_complete", s.handleIsComplete)
	mux.HandleFunc("/mrp/v1/format", s.handleFormat)
	mux.HandleFunc("/mrp/v1/history", s.handleHistory)
	mux.HandleFunc("/mrp/v1/assets/", s.handleAssets)
	return withCORS(mux)
}

func (s *Server) handleCapabilities(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodGet) {
		return
	}
	shell := s.bashPath
	writeJSON(w, http.StatusOK, Capabilities{
		Runtime:   "bash",
		Version:   s.bashVersion,
		Languages: []string{"bash", "sh", "shell"},
		Features: Features{
			Execute:        true,
			ExecuteStream:  true,
			Interrupt:      true,
			Complete:       true,
			Inspect:        false,
			Hover:          true,
			Variables:      true,
			VariableExpand: false,
			Reset:          true,
			IsComplete:     true,
			Format:         false,
			History:        true,
			Assets:         false,
		},
		Environment: &Environment{CWD: s.cwd, Executable: s.bashPath, Shell: &shell},
	})
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	if err := s.runtime.Reset(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"success": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"success": true})
}

func (s *Server) handleExecute(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req ExecuteRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	result := worker.Execute(req.Code, req.StoreHistoryValue(), req.ExecID)
	writeJSON(w, http.StatusOK, result)
}

func (s *Server) handleExecuteStream(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req ExecuteRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}

	execID := req.ExecID
	if execID == "" {
		execID = fmt.Sprintf("exec-%d", time.Now().UnixNano())
	}

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": "streaming not supported by response writer"})
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")

	_ = writeSSE(w, "start", map[string]any{"execId": execID, "timestamp": time.Now().UTC().Format(time.RFC3339Nano)})
	flusher.Flush()

	events := make(chan SSEEvent, 128)

	go func() {
		defer close(events)
		result := worker.ExecuteStreaming(
			req.Code,
			req.StoreHistoryValue(),
			execID,
			func(stream, chunk, accumulated string) {
				events <- SSEEvent{Type: stream, Data: map[string]any{"content": chunk, "accumulated": accumulated}}
			},
			func(stdinReq StdinRequest) (string, error) {
				inputCh := s.runtime.RegisterPendingInput(execID)
				defer s.runtime.ClearPendingInput(execID)
				events <- SSEEvent{Type: "stdin_request", Data: stdinReq}
				select {
				case response := <-inputCh:
					if response.Cancelled {
						return "", ErrInputCancelled
					}
					return response.Text, nil
				case <-r.Context().Done():
					return "", ErrInputCancelled
				}
			},
		)

		if result.Success {
			events <- SSEEvent{Type: "result", Data: result}
		} else if result.Error != nil {
			events <- SSEEvent{Type: "error", Data: map[string]any{
				"type":           result.Error.Type,
				"message":        result.Error.Message,
				"traceback":      result.Error.Traceback,
				"stdout":         result.Stdout,
				"stderr":         result.Stderr,
				"executionCount": result.ExecutionCount,
				"duration":       result.Duration,
			}}
		} else {
			events <- SSEEvent{Type: "error", Data: map[string]any{"type": "ExecutionError", "message": "execution failed"}}
		}
	}()

	pingTicker := time.NewTicker(30 * time.Second)
	defer pingTicker.Stop()

	for {
		select {
		case event, ok := <-events:
			if !ok {
				_ = writeSSE(w, "done", map[string]any{})
				flusher.Flush()
				return
			}
			if err := writeSSE(w, event.Type, event.Data); err != nil {
				return
			}
			flusher.Flush()
		case <-pingTicker.C:
			if err := writeSSE(w, "ping", map[string]any{}); err != nil {
				return
			}
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func (s *Server) handleInput(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req InputRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"accepted": false, "error": err.Error()})
		return
	}
	if req.ExecID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"accepted": false, "error": "exec_id is required"})
		return
	}
	accepted := s.runtime.ProvideInput(req.ExecID, req.Text)
	if !accepted {
		writeJSON(w, http.StatusOK, map[string]any{"accepted": false, "error": "No pending input request"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"accepted": true})
}

func (s *Server) handleInputCancel(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req InputRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"cancelled": false, "error": err.Error()})
		return
	}
	if req.ExecID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]any{"cancelled": false, "error": "exec_id is required"})
		return
	}
	cancelled := s.runtime.CancelInput(req.ExecID)
	if !cancelled {
		writeJSON(w, http.StatusOK, map[string]any{"cancelled": false, "error": "No pending input request"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"cancelled": true})
}

func (s *Server) handleInterrupt(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"interrupted": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"interrupted": worker.Interrupt()})
}

func (s *Server) handleComplete(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req CompleteRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	cursor := req.Cursor
	if cursor == 0 && req.Code != "" {
		cursor = len(req.Code)
	}
	writeJSON(w, http.StatusOK, worker.Complete(req.Code, cursor))
}

func (s *Server) handleInspect(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"found": false, "source": "runtime", "error": "Inspection not supported for bash"})
}

func (s *Server) handleHover(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req HoverRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	cursor := req.Cursor
	if cursor == 0 && req.Code != "" {
		cursor = len(req.Code)
	}
	writeJSON(w, http.StatusOK, worker.Hover(req.Code, cursor))
}

func (s *Server) handleVariables(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req VariablesRequest
	if err := decodeJSON(r, &req, true); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, worker.GetVariables(req.Filter.NamePattern))
}

func (s *Server) handleVariableDetail(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	name := strings.TrimPrefix(r.URL.Path, "/mrp/v1/variables/")
	if decoded, err := url.PathUnescape(name); err == nil {
		name = decoded
	}
	if name == "" {
		http.NotFound(w, r)
		return
	}
	var req VariableDetailRequest
	if err := decodeJSON(r, &req, true); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	maxValueLength := 1000
	if req.MaxValueLength != nil {
		maxValueLength = *req.MaxValueLength
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, worker.GetVariableDetail(name, req.Path, maxValueLength))
}

func (s *Server) handleIsComplete(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req IsCompleteRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, worker.IsComplete(req.Code))
}

func (s *Server) handleFormat(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req FormatRequest
	if err := decodeJSON(r, &req, false); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"formatted": req.Code, "changed": false, "error": "Formatting not supported for bash"})
}

func (s *Server) handleHistory(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodPost) {
		return
	}
	var req HistoryRequest
	if err := decodeJSON(r, &req, true); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	worker, err := s.runtime.Worker()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}

	n := 20
	if req.N != nil && *req.N > 0 {
		n = *req.N
	}
	pattern := ""
	if req.Pattern != nil {
		pattern = *req.Pattern
	}

	writeJSON(w, http.StatusOK, worker.GetHistory(n, pattern, req.Before))
}

func (s *Server) handleAssets(w http.ResponseWriter, r *http.Request) {
	if !requireMethod(w, r, http.MethodGet) {
		return
	}
	http.Error(w, "Assets not supported for bash runtime", http.StatusNotFound)
}

func detectBashVersion() string {
	cmd := exec.Command("bash", "--version")
	out, err := cmd.Output()
	if err != nil {
		return "unknown"
	}
	lines := strings.Split(string(out), "\n")
	for _, line := range lines {
		lower := strings.ToLower(line)
		if !strings.Contains(lower, "version") {
			continue
		}
		parts := strings.SplitN(line, "version", 2)
		if len(parts) == 2 {
			fields := strings.Fields(strings.TrimSpace(parts[1]))
			if len(fields) > 0 {
				return fields[0]
			}
		}
	}
	return "unknown"
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeSSE(w http.ResponseWriter, event string, data any) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, payload)
	return err
}

func decodeJSON(r *http.Request, dst any, allowEmpty bool) error {
	if r.Body == nil {
		if allowEmpty {
			return nil
		}
		return errors.New("request body is required")
	}
	defer r.Body.Close()
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return err
	}
	if len(strings.TrimSpace(string(body))) == 0 {
		if allowEmpty {
			return nil
		}
		return errors.New("request body is required")
	}
	return json.Unmarshal(body, dst)
}

func requireMethod(w http.ResponseWriter, r *http.Request, method string) bool {
	if r.Method == method {
		return true
	}
	w.Header().Set("Allow", method)
	http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	return false
}

func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Credentials", "true")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *Server) Shutdown(ctx context.Context) error {
	_ = ctx
	return s.runtime.Shutdown()
}
