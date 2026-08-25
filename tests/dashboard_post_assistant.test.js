const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const template = fs.readFileSync("dashboard/templates/dashboard.html", "utf8");

function extractFunction(name) {
    const start = template.indexOf(`function ${name}(`);
    assert.notEqual(start, -1, `Expected ${name} to exist in the dashboard script.`);

    const bodyStart = template.indexOf("{", start);
    let depth = 0;
    for (let index = bodyStart; index < template.length; index += 1) {
        if (template[index] === "{") depth += 1;
        if (template[index] === "}") depth -= 1;
        if (depth === 0) return template.slice(start, index + 1);
    }
    throw new Error(`Could not extract ${name} from the dashboard script.`);
}

function element() {
    return {
        children: [],
        className: "",
        dataset: {},
        style: {},
        textContent: "",
        innerHTML: "",
        appendChild(child) { this.children.push(child); },
        addEventListener() {},
        querySelectorAll() { return []; },
    };
}

function dashboardElement() {
    const target = element();
    target.classList = { add() {}, remove() {}, toggle() {} };
    target.querySelector = () => dashboardElement();
    return new Proxy(target, {
        get(value, property) {
            if (property in value) return value[property];
            return () => {};
        },
    });
}

test("dashboard script boots without undefined function or element references", async () => {
    const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
    const elements = new Map();
    const errors = [];
    const getElement = id => {
        if (!elements.has(id)) elements.set(id, dashboardElement());
        return elements.get(id);
    };
    const context = {
        document: {
            addEventListener() {},
            createElement: dashboardElement,
            getElementById: getElement,
            querySelectorAll: () => [],
        },
        window: { addEventListener() {}, innerWidth: 1024, scrollTo() {} },
        navigator: { clipboard: { writeText: async () => {} } },
        URL: { createObjectURL: () => "blob:test", revokeObjectURL() {} },
        fetch: async url => ({
            ok: true,
            json: async () => String(url).includes("/stories")
                ? { stories: [] }
                : { account: {}, totals: {}, top_posts: [], advanced: {} },
        }),
        setInterval() {},
        setTimeout,
        console: { error: (...args) => errors.push(args.join(" ")) },
    };

    vm.runInNewContext(script, context);
    await new Promise(resolve => setTimeout(resolve, 0));

    assert.deepEqual(errors, []);
});

test("music suggestions escape generated track metadata before rendering", () => {
    const musicSuggestions = element();
    const context = {
        document: {
            getElementById(id) {
                return id === "musicSuggestions" ? musicSuggestions : null;
            },
        },
    };

    vm.runInNewContext(
        `${extractFunction("escapeHtml")}\n${extractFunction("renderMusicSuggestions")}`,
        context,
    );
    context.renderMusicSuggestions([{
        track: '<img src=x onerror="alert(1)">',
        artist: "Artist & Co.",
        reason: "It's a \"test\" track.",
    }]);

    assert.match(musicSuggestions.innerHTML, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
    assert.match(musicSuggestions.innerHTML, /Artist &amp; Co\./);
    assert.doesNotMatch(musicSuggestions.innerHTML, /<img/);
});

test("Story history renders a scheduled Story without missing helpers", () => {
    const storiesList = element();
    const storiesStatus = element();
    const context = {
        document: {
            getElementById(id) {
                return id === "storiesList" ? storiesList : storiesStatus;
            },
            createElement: element,
        },
    };

    vm.runInNewContext(
        `${extractFunction("formatStoryDate")}\n${extractFunction("renderStories")}`,
        context,
    );
    context.renderStories([{
        id: "story-1",
        media_kind: "image",
        media_url: "https://example.test/story.jpg",
        status: "scheduled",
        created_at: "2026-08-25T10:00:00+00:00",
        scheduled_local: "2026-08-25T20:00:00+01:00",
    }]);

    assert.equal(storiesStatus.textContent, "1 Story item(s).");
    assert.equal(storiesList.children.length, 1);
});
