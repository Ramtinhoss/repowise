import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  AddRepoWizard,
  type AddRepoWizardAdapter,
  type AddRepoPreflightResult,
} from "../../src/onboarding/add-repo-wizard.js";

function makePreflight(overrides?: Partial<AddRepoPreflightResult>): AddRepoPreflightResult {
  return {
    provider: { ok: true, name: "gemini", model: "gemini-flash", error: null },
    file_count: 120,
    estimate: {
      total_pages: 40,
      estimated_cost_usd: 0.8,
      cost_low_usd: 0.5,
      cost_high_usd: 1.2,
      is_calibrated: false,
    },
    ...overrides,
  };
}

function makeAdapter(overrides?: Partial<AddRepoWizardAdapter>): AddRepoWizardAdapter {
  return {
    createRepo: vi.fn().mockResolvedValue({ id: "r1", name: "demo" }),
    preflight: vi.fn().mockResolvedValue(makePreflight()),
    startIndex: vi.fn().mockResolvedValue({ job_id: "job-1" }),
    onDone: vi.fn(),
    settingsHref: "/settings",
    ...overrides,
  };
}

async function fillAndSubmitDetails() {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "demo" } });
  fireEvent.change(screen.getByLabelText("Local Path"), {
    target: { value: "C:\\repos\\demo" },
  });
  fireEvent.click(screen.getByRole("button", { name: /Continue/ }));
}

describe("AddRepoWizard", () => {
  it("registers without indexing, then auto-starts when the estimate clears the cost gate", async () => {
    const adapter = makeAdapter();
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await fillAndSubmitDetails();

    await waitFor(() => expect(adapter.onDone).toHaveBeenCalledWith("r1", "job-1"));
    expect(adapter.createRepo).toHaveBeenCalledWith(
      expect.objectContaining({ name: "demo", local_path: "C:\\repos\\demo" }),
    );
    expect(adapter.preflight).toHaveBeenCalledWith("r1");
    expect(adapter.startIndex).toHaveBeenCalledWith("r1");
  });

  it("stops for explicit confirmation above the cost gate", async () => {
    const adapter = makeAdapter({
      preflight: vi.fn().mockResolvedValue(
        makePreflight({
          estimate: {
            total_pages: 900,
            estimated_cost_usd: 7.4,
            cost_low_usd: 5.0,
            cost_high_usd: 11.0,
            is_calibrated: true,
          },
        }),
      ),
    });
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await fillAndSubmitDetails();

    expect(await screen.findByText(/Estimated generation cost/)).toBeTruthy();
    expect(screen.getByText(/\$5\.00 - \$11\.00/)).toBeTruthy();
    expect(adapter.startIndex).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Start indexing/ }));
    await waitFor(() => expect(adapter.startIndex).toHaveBeenCalledWith("r1"));
    await waitFor(() => expect(adapter.onDone).toHaveBeenCalledWith("r1", "job-1"));
  });

  it("surfaces a broken provider with recovery paths and never starts a job", async () => {
    const adapter = makeAdapter({
      preflight: vi.fn().mockResolvedValue(
        makePreflight({
          provider: { ok: false, name: "gemini", model: null, error: "invalid API key" },
          estimate: null,
        }),
      ),
    });
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await fillAndSubmitDetails();

    expect(await screen.findByText(/gemini check failed/)).toBeTruthy();
    expect(screen.getByText(/invalid API key/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Provider settings/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Retry check/ })).toBeTruthy();
    expect(adapter.startIndex).not.toHaveBeenCalled();

    // "Finish without indexing" still lands the user on the registered repo.
    fireEvent.click(screen.getByRole("button", { name: /Finish without indexing/ }));
    expect(adapter.onDone).toHaveBeenCalledWith("r1", null);
  });

  it("anchors path-shaped registration failures to the path field", async () => {
    const adapter = makeAdapter({
      createRepo: vi
        .fn()
        .mockRejectedValue(new Error("local_path is not a git repository")),
    });
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await fillAndSubmitDetails();

    expect(await screen.findByText(/not a git repository/)).toBeTruthy();
    expect(adapter.preflight).not.toHaveBeenCalled();
    // Still on the details step, ready to correct the path.
    expect(screen.getByLabelText("Local Path")).toBeTruthy();
  });
});

describe("AddRepoWizard — adding from a URL", () => {
  function makeRemoteAdapter(overrides?: Partial<AddRepoWizardAdapter>) {
    return makeAdapter({
      createRepoFromUrl: vi.fn().mockResolvedValue({ id: "r2", name: "app" }),
      ...overrides,
    });
  }

  // Radix tabs select on mousedown (or focus), not on a bare click event.
  function selectRemoteTab() {
    const tab = screen.getByRole("tab", { name: /From URL/ });
    fireEvent.mouseDown(tab);
    fireEvent.click(tab);
  }

  async function submitRemote(url: string) {
    selectRemoteTab();
    fireEvent.change(screen.getByLabelText("Repository URL"), { target: { value: url } });
    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));
  }

  it("hides the URL source when the adapter cannot clone", () => {
    // A client that only reaches its own filesystem should not offer a
    // source it has no way to satisfy.
    render(<AddRepoWizard adapter={makeAdapter()} open onOpenChange={vi.fn()} />);
    expect(screen.queryByRole("tab", { name: /From URL/ })).toBeNull();
    expect(screen.getByLabelText("Local Path")).toBeTruthy();
  });

  it("clones, registers, then runs the same preflight and index steps", async () => {
    const adapter = makeRemoteAdapter();
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await submitRemote("https://github.com/acme/app");

    await waitFor(() => expect(adapter.onDone).toHaveBeenCalledWith("r2", "job-1"));
    expect(adapter.createRepoFromUrl).toHaveBeenCalledWith(
      expect.objectContaining({ url: "https://github.com/acme/app", name: "app" }),
      expect.any(Function),
    );
    // The local-path registration path must not also fire.
    expect(adapter.createRepo).not.toHaveBeenCalled();
    expect(adapter.preflight).toHaveBeenCalledWith("r2");
    expect(adapter.startIndex).toHaveBeenCalledWith("r2");
  });

  it("derives the name from the URL until the operator overrides it", async () => {
    const adapter = makeRemoteAdapter();
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    selectRemoteTab();
    const urlField = screen.getByLabelText("Repository URL");

    fireEvent.change(urlField, { target: { value: "git@github.com:acme/my-app.git" } });
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("my-app");

    // Once typed by hand, further URL edits must not clobber it.
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "custom" } });
    fireEvent.change(urlField, { target: { value: "https://github.com/acme/other" } });
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("custom");
  });

  it("passes a token through for private repos and shows clone progress", async () => {
    let report: ((m: string) => void) | undefined;
    const adapter = makeRemoteAdapter({
      createRepoFromUrl: vi.fn().mockImplementation((_input, onProgress) => {
        report = onProgress;
        return new Promise(() => {}); // never settles: hold the cloning step
      }),
    });
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    selectRemoteTab();
    fireEvent.change(screen.getByLabelText("Repository URL"), {
      target: { value: "https://github.com/acme/private-app" },
    });
    fireEvent.change(screen.getByLabelText(/Access token/), {
      target: { value: "ghp_token" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Continue/ }));

    expect(await screen.findByText(/Cloning repository/)).toBeTruthy();
    expect(adapter.createRepoFromUrl).toHaveBeenCalledWith(
      expect.objectContaining({ access_token: "ghp_token" }),
      expect.any(Function),
    );

    // Progress messages from the server reach the dialog.
    report?.("Cloning acme/private-app");
    expect(await screen.findByText(/Cloning acme\/private-app/)).toBeTruthy();
  });

  it("returns to the form with the failure anchored to the URL field", async () => {
    const adapter = makeRemoteAdapter({
      createRepoFromUrl: vi
        .fn()
        .mockRejectedValue(new Error("git clone failed: repository not found")),
    });
    render(<AddRepoWizard adapter={adapter} open onOpenChange={vi.fn()} />);

    await submitRemote("https://github.com/acme/missing");

    expect(await screen.findByText(/repository not found/)).toBeTruthy();
    expect(screen.getByLabelText("Repository URL")).toBeTruthy();
    expect(adapter.preflight).not.toHaveBeenCalled();
  });
});
