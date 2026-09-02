package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func main() {
	appDir, err := filepath.Abs(filepath.Dir(os.Args[0]))
	if err != nil {
		fail("Could not determine the application directory: " + err.Error())
	}
	fmt.Println("==============================================")
	fmt.Println(" Finance Dashboard - Startup")
	fmt.Println("==============================================")
	fmt.Println()

	requirements := filepath.Join(appDir, "requirements.txt")
	app := filepath.Join(appDir, "finance_dashboard.py")
	msix := filepath.Join(os.TempDir(), "python-manager-26.3.msix")

	if !fileExists(requirements) || !fileExists(app) {
		fail("The launcher must be in the same folder as finance_dashboard.py and requirements.txt.")
	}

	fmt.Println("[1/5] Checking Python...")
	py := findPythonLauncher()
	if py == "" {
		fmt.Println("      Python Install Manager not found.")
		fmt.Println("      Downloading Python Install Manager 26.3 from python.org...")

		const managerURL = "https://www.python.org/ftp/python/pymanager/python-manager-26.3.msix"
		const managerSHA256 = "bdd1a4e0b485f674748b275095cc8e7132beb37a1ae91db080abb2661e3badec"

		// Download the official MSIX to the Windows temporary directory.
		// It is deleted after installation.
		if err := downloadFile(managerURL, msix); err != nil {
			fail("Could not download the Python Install Manager.\n\n" + err.Error())
		}

		// Verify the downloaded file before passing it to Windows.
		if err := verifySHA256(msix, managerSHA256); err != nil {
			_ = os.Remove(msix)
			fail("The downloaded Python Install Manager failed its integrity check.\n\n" + err.Error())
		}

		fmt.Println("      Installing Python Install Manager...")
		if err := run("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
			"Add-AppxPackage -Path '"+escapePS(msix)+"'"); err != nil {
			_ = os.Remove(msix)
			fail("Could not install the downloaded Python Install Manager.\n\n" + err.Error())
		}
		_ = os.Remove(msix)
		py = findPythonLauncher()
	}
	if py == "" {
		fail("Python Install Manager was installed, but its py.exe could not be located.")
	}

	// Make sure Python 3.14 exists. Python Install Manager 26.3 bundles
	// Python 3.14.5, so the manager can install the required runtime.
	if !python314Available(py) {
		fmt.Println("      Installing Python 3.14...")
		if err := run(py, "install", "3.14"); err != nil {
			fail("Could not install Python 3.14.\n\n" + err.Error())
		}
	}
	fmt.Println("      Python 3.14 is ready.")

	venv := filepath.Join(appDir, ".venv")
	venvPython := filepath.Join(venv, "Scripts", "python.exe")

	fmt.Println("[2/5] Checking application environment...")
	if !fileExists(venvPython) {
		fmt.Println("      Creating local virtual environment...")
		if err := run(py, "-3.14", "-m", "venv", venv); err != nil {
			fail("Could not create the application's virtual environment.\n\n" + err.Error())
		}
	} else {
		fmt.Println("      Existing virtual environment found.")
	}

	fmt.Println("[3/5] Checking dependencies...")
	reqHash, err := sha256File(requirements)
	if err != nil {
		fail("Could not read requirements.txt: " + err.Error())
	}
	stamp := filepath.Join(venv, ".requirements.sha256")
	oldHash := ""
	if b, e := os.ReadFile(stamp); e == nil {
		oldHash = strings.TrimSpace(string(b))
	}

	if oldHash != reqHash {
		fmt.Println("      Installing/updating required packages...")
		if err := run(venvPython, "-m", "pip", "install", "-r", requirements); err != nil {
			fail("Dependency installation failed.\n\n" + err.Error())
		}
		_ = os.WriteFile(stamp, []byte(reqHash+"\n"), 0644)
	} else {
		fmt.Println("      Dependencies are already up to date.")
	}

	fmt.Println("[4/5] Starting Streamlit...")
	fmt.Println()
	fmt.Println("      The dashboard will open in your web browser.")
	fmt.Println("      Keep this window open while using the dashboard.")
	fmt.Println()

	// Streamlit is launched through the venv's Python rather than relying on
	// PATH. This is more reliable than calling a global "streamlit" command.
	// Run Streamlit in headless mode and explicitly open the browser once
	// the server is actually ready. This is more reliable than relying on
	// Streamlit's automatic browser launch when started from an .exe.
	port := "8501"
	url := "http://localhost:" + port

	cmd := exec.Command(venvPython, "-m", "streamlit", "run", app,
		"--server.headless=true",
		"--server.port="+port,
		"--browser.gatherUsageStats=false",
	)
	cmd.Dir = appDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if err := cmd.Start(); err != nil {
		fail("Could not start Streamlit.\n\n" + err.Error())
	}

	// Wait up to 30 seconds for Streamlit to start responding.
	browserOpened := false
	client := &http.Client{Timeout: 500 * time.Millisecond}

	for i := 0; i < 60; i++ {
		time.Sleep(500 * time.Millisecond)

		resp, err := client.Get(url)
		if err == nil {
			resp.Body.Close()

			// Open the user's default browser.
			if err := exec.Command("cmd", "/c", "start", "", url).Start(); err == nil {
				browserOpened = true
			}
			break
		}
	}

	if !browserOpened {
		fmt.Println("      Could not automatically open the browser.")
		fmt.Println("      Open this address manually: " + url)
	} else {
		fmt.Println("      Dashboard opened in your default browser.")
	}

	// Keep the launcher attached to Streamlit so the console remains open
	// and Streamlit's output/errors remain visible.
	if err := cmd.Wait(); err != nil {
		fail("Streamlit stopped with an error.\n\n" + err.Error())
	}

	fmt.Println()
	fmt.Println("[5/5] Dashboard closed.")
	fmt.Println("Press Enter to exit.")
	bufio.NewReader(os.Stdin).ReadString('\n')
}

func findPythonLauncher() string {
	// Prefer the Python Install Manager if it is already installed. This
	// avoids accidentally selecting the old/legacy py.exe launcher.
	ps := `(Get-AppxPackage -Name PythonSoftwareFoundation.PythonManager -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty InstallLocation)`
	out, err := exec.Command("powershell.exe", "-NoProfile", "-Command", ps).Output()
	if err == nil {
		loc := strings.TrimSpace(string(out))
		if loc != "" {
			for _, name := range []string{"py.exe", "pymanager.exe", "python.exe"} {
				p := filepath.Join(loc, name)
				if fileExists(p) && testCommand(p, "--version") {
					return p
				}
			}
		}
	}

	// Then use an existing Python manager available on PATH.
	for _, name := range []string{"pymanager.exe"} {
		if p, err := exec.LookPath(name); err == nil && testCommand(p, "--version") {
			return p
		}
	}

	// Finally accept an existing Python/py installation. If it is a legacy
	// py.exe and Python 3.14 is not available, the caller will install the
	// bundled manager rather than trying unsupported "py install" commands.
	for _, name := range []string{"python.exe", "python", "py.exe", "py"} {
		if p, err := exec.LookPath(name); err == nil && python314Available(p) {
			return p
		}
	}
	return ""
}

func python314Available(py string) bool {
	return testCommand(py, "-3.14", "--version")
}

func testCommand(exe string, args ...string) bool {
	cmd := exec.Command(exe, args...)
	return cmd.Run() == nil
}

func run(exe string, args ...string) error {
	cmd := exec.Command(exe, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin
	return cmd.Run()
}

func fileExists(p string) bool {
	info, err := os.Stat(p)
	return err == nil && !info.IsDir()
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	buf := make([]byte, 64*1024)
	for {
		n, e := f.Read(buf)
		if n > 0 {
			_, _ = h.Write(buf[:n])
		}
		if e != nil {
			if e == io.EOF {
				break
			}
			break
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func downloadFile(url, destination string) error {
	client := &http.Client{Timeout: 10 * time.Minute}
	resp, err := client.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP download failed with status %s", resp.Status)
	}

	f, err := os.Create(destination)
	if err != nil {
		return err
	}
	defer f.Close()

	_, err = io.Copy(f, resp.Body)
	return err
}

func verifySHA256(path, expected string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return err
	}

	actual := hex.EncodeToString(h.Sum(nil))
	if !strings.EqualFold(actual, expected) {
		return fmt.Errorf("expected SHA-256 %s, got %s", expected, actual)
	}
	return nil
}

func escapePS(s string) string {
	return strings.ReplaceAll(s, "'", "''")
}

func fail(msg string) {
	fmt.Fprintln(os.Stderr, "\nERROR:\n"+msg)
	fmt.Fprintln(os.Stderr, "\nPress Enter to close.")
	bufio.NewReader(os.Stdin).ReadString('\n')
	os.Exit(1)
}

// Keep syscall imported so Windows builds can be extended with native UI
// without changing the source layout.
var _ = syscall.Handle(0)
