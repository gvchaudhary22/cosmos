# Skill: Instructor v2.0
> Turn any complex codebase into a self-explaining, masterclass-level experience

## ACTIVATION
Triggered when the user asks for an explanation, deep dive, or overview of a concept, or when the `pedagogue` agent is dispatched.

## CORE PRINCIPLES
1. **The Three-Pillar Mandate**: Every major concept explanation MUST include:
   - **The Control Plane** (The rules/config): Where is this behavior governed?
   - **The Execution Layer** (The workers/code): Where is the logic implemented?
   - **The Persistence Layer** (The state/logs): Where is the result memorialized?
2. **Analogy-First Architecture**: Always start complex explanations with a relatable real-world comparison.
3. **Evidence-Based Tracing**: Concept claims are invalid without file links to the specific code that proves them.
4. **Visual-First Strategy**: Use Mermaid diagrams for any multi-step process. No exceptions for complex logic.
5. **Proactive Clarity**: Include an "Anticipatory FAQ" to answer the 3 most likely follow-up questions (Why? What if it fails? Where is the data?).

## PATTERNS

### The "One-Shot" Masterclass
1. **The Analogy**: A relatable comparison to build the first mental model.
2. **The Tech-Stack Matrix**: A table mapping the concept to the Three Pillars.
3. **The Lifecycle Diagram**: A Mermaid diagram showing the "Happy Path" and "Failure Path."
4. **The Code-Trace**: Deep links to the specific logic on disk.
5. **The Anticipatory FAQ**: Three proactive answers to hidden complexities.

### The Repo-Agnostic Walkthrough
- Identify the repo's entry point (`app/main.py` for COSMOS).
- Trace the request flow from "User Intent" to "Final Result."
- Explain the middleware/hooks layer (the "invisible logic").

## CHECKLISTS
- [ ] Covered all Three Pillars (Control, Execution, Persistence)?
- [ ] Analogy included and accurate?
- [ ] Mermaid diagram provided for multi-step flows?
- [ ] File links are absolute and point to actual code?
- [ ] Anticipatory FAQ answers at least 3 queries?

## ANTI-PATTERNS
- **Wall of Text**: Explaining without visual or structural breaks.
- **Assumed Context**: Explaining a feature without grounding it in the repo's file structure.
- **Progressive Delay**: Holding back information that makes the first explanation incomplete.
- **Generic AI Boilerplate**: Explaining how something works theoretically instead of how it works in *this* repo's code.

## VERIFICATION WORKFLOW
1. **The "New Dev" Test**: Could a new developer join the team and implement this feature based only on this report?
2. **Link Integrity**: Are the file URIs pointing to active code?
3. **Completeness Score**: Does the answer require a follow-up "How?" or "Where?"
