import { fireEvent, render } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { WebResultsProps } from "../../types/ws";
import { WebResultsCard } from "./WebResultsCard";

/** Canonical fixture mirroring a `web_search` deliverable: a direct answer +
 * two ranked sources, the shape the backend `to_web_results_props` emits. */
const FIXTURE: WebResultsProps = {
  query: "python gil",
  answer: "The GIL is a mutex that protects access to Python objects.",
  results: [
    {
      title: "Understanding the GIL",
      url: "https://realpython.com/python-gil/",
      snippet: "The GIL is a single lock…",
    },
    {
      title: "PEP 703 — making the GIL optional",
      url: "https://peps.python.org/pep-0703/",
      snippet: "A path to a free-threaded CPython.",
    },
  ],
};

describe("WebResultsCard", () => {
  test("renders the chrome-free web body (no overlay chrome)", () => {
    const { container } = render(<WebResultsCard props={FIXTURE} />);
    expect(container.querySelector(".ov-web")).not.toBeNull();
    expect(container.querySelector(".ov-corner")).toBeNull();
    expect(container.querySelector(".ov-header")).toBeNull();
  });

  test("renders the answer lead + one row per result (title / url / snippet)", () => {
    const { container } = render(<WebResultsCard props={FIXTURE} />);
    expect(container.querySelector(".ov-web-answer")?.textContent).toContain("GIL is a mutex");
    const items = container.querySelectorAll(".ov-web-item");
    expect(items.length).toBe(2);
    expect(items[0].querySelector(".ov-web-title")?.textContent).toContain("Understanding the GIL");
    // The URL is compacted to host + path (scheme dropped).
    expect(items[0].querySelector(".ov-web-url")?.textContent).toBe("realpython.com/python-gil/");
    expect(items[0].querySelector(".ov-web-snippet")?.textContent).toContain("single lock");
  });

  test("clicking a result title opens its url via the seam", () => {
    const openExternal = vi.fn();
    const { container } = render(<WebResultsCard props={FIXTURE} openExternal={openExternal} />);
    const title = container.querySelector<HTMLButtonElement>(".ov-web-title");
    if (title) fireEvent.click(title);
    expect(openExternal).toHaveBeenCalledTimes(1);
    expect(openExternal).toHaveBeenCalledWith("https://realpython.com/python-gil/");
  });

  test("omits the answer lead when absent", () => {
    const { container } = render(<WebResultsCard props={{ ...FIXTURE, answer: undefined }} />);
    expect(container.querySelector(".ov-web-answer")).toBeNull();
    expect(container.querySelectorAll(".ov-web-item").length).toBe(2);
  });

  test("drops malformed result entries (no url), keeps the valid ones", () => {
    const props = {
      query: "q",
      results: [{ title: "no url" }, { title: "ok", url: "https://ok.com" }],
    };
    const { container } = render(<WebResultsCard props={props} />);
    const items = container.querySelectorAll(".ov-web-item");
    expect(items.length).toBe(1);
    expect(items[0].querySelector(".ov-web-title")?.textContent).toContain("ok");
  });

  test("title falls back to the url when missing", () => {
    const props = { query: "q", results: [{ url: "https://only-url.com" }] };
    const { container } = render(<WebResultsCard props={props} />);
    expect(container.querySelector(".ov-web-title")?.textContent).toContain("https://only-url.com");
  });

  test("renders nothing for a malformed props bag (no query / results)", () => {
    const { container } = render(<WebResultsCard props={{ foo: "bar" }} />);
    expect(container.firstChild).toBeNull();
  });
});
