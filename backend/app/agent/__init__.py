"""
Agent core — built phase by phase as small, independently testable modules:

    cloner       (Phase 2)  clone a repo into an isolated temp dir, with guards
    analyzer     (Phase 2)  detect language/framework -> structured RepoProfile
    generator    (Phase 3)  Gemini structured-output Dockerfile generation
    builder      (Phase 4)  docker build with streamed logs
    runner       (Phase 5)  docker run + verify (http/tcp/log/exit)
    orchestrator (Phase 4+) drive the clone->...->verify loop with retries

Empty for now; each module lands in its phase.
"""
