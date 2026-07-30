/**
 * Drive the REAL saveSettings() out of an app.js copy under a stub DOM and
 * print the JSON body it would POST.
 *
 * No browser and no jsdom: the function is extracted verbatim (same algorithm
 * tests/test_watchlist_guard.py uses) and evaluated with only the globals it
 * actually reaches for. That keeps the assertion on the shipped source rather
 * than on a paraphrase of it - a static regex cannot tell "present and false"
 * apart from "absent", which is the whole distinction under test.
 *
 * Usage:  node drive_save_settings.mjs <path-to-app.js> <benefits-json|null>
 *
 * The second argument is the state of the four benefit checkboxes:
 *   '{"BADGE": false, ...}'  - only the named controls exist, with those values
 *   'null'                   - the Settings tab never rendered; none exist
 */
import { readFileSync } from 'node:fs';

function extractFunction(source, name) {
    const declaration = new RegExp(`(?:\\basync\\s+)?\\bfunction\\s+${name}\\s*\\(`).exec(source);
    if (!declaration) throw new Error(`function ${name}() not found`);
    const rest = source.slice(declaration.index);
    const closing = /\n\}/.exec(rest);
    if (!closing) throw new Error(`function ${name}() has no column-0 closing brace`);
    const body = rest.slice(0, closing.index + closing[0].length);
    const open = (body.match(/\{/g) || []).length;
    const shut = (body.match(/\}/g) || []).length;
    if (open !== shut) throw new Error(`mis-extracted body for ${name}(): ${open} vs ${shut}`);
    return body;
}

// Every control saveSettings() reads, minus the four benefit checkboxes, which the
// scenario supplies. The values are irrelevant here; the PRESENCE is not - #dark-mode,
// #connection-quality and #minimum-refresh-interval are read without `?.`.
const BASE_DOM = () => ({
    'language': { selectedIndex: 0, value: 'English' },
    'dark-mode': { checked: true },
    'connection-quality': { value: '1' },
    'minimum-refresh-interval': { value: '30' },
    'discord-webhook-drops': { value: '' },
    'discord-webhook-points': { value: '' },
    'discord-webhook-mentions': { value: '' },
    'claim-channel-points': { checked: true },
    'idle-use-followed': { checked: false },
    'idle-parallel': { checked: true },
    'drop-blacklist-input': { value: '' },
    'scheduler-enabled': { checked: false },
    'scheduler-start': { value: '22:00' },
    'scheduler-stop': { value: '08:00' },
    'auto-prioritize-toggle': { checked: false },
    'auto-add-linked-toggle': { checked: false },
    'auto-clean-watchlist-toggle': { checked: false },
    'tab-counter-toggle': { checked: true },
    'set-make-predictions': { checked: false },
    'set-bet-strategy': { value: 'SMART' },
    'set-bet-pct': { value: '5' },
    'set-bet-max': { value: '50000' },
    'set-bet-min': { value: '1000' },
    'set-bet-delay': { value: '30' },
});

const BENEFIT_IDS = {
    DIRECT_ENTITLEMENT: 'mining-benefit-item',
    BADGE: 'mining-benefit-badge',
    EMOTE: 'mining-benefit-emote',
    UNKNOWN: 'mining-benefit-unknown',
};

function domFor(benefits) {
    const dom = BASE_DOM();
    if (benefits !== null) {
        for (const [type, id] of Object.entries(BENEFIT_IDS)) {
            if (type in benefits) dom[id] = { checked: benefits[type] };
        }
    }
    return dom;
}

async function run(copyPath, benefits) {
    const source = extractFunction(readFileSync(copyPath, 'utf8'), 'saveSettings');
    const dom = domFor(benefits);
    let posted = null;

    const sandbox = {
        API_BASE: '',
        document: { getElementById: (id) => dom[id] ?? null },
        state: {
            settingsLoaded: true,
            settings: {
                language: 'English', games_to_watch: ['Overwatch'], proxy: '',
                idle_channels: [], preferred_games: [],
            },
            serverGamesToWatch: ['Overwatch'],
        },
        getInventoryFilters: () => ({}),
        getPredChannels: () => [],
        getChannelStrategies: () => ({}),
        fetch: async (_url, init) => { posted = init.body; return { ok: true, json: async () => ({}) }; },
        reconcileSettingsSave: async () => true,
        console: { log() {}, warn() {}, error(...args) { throw new Error(args.join(' ')); } },
    };
    const names = Object.keys(sandbox);
    // The evaluated string is this repository's own app.js, read from a path the
    // test supplies - executing it IS the test. Nothing here ever sees network,
    // request or user input, and the harness is never shipped or served.
    const factory = new Function(...names, `${source}\nreturn saveSettings;`);
    await factory(...names.map((name) => sandbox[name]))({});
    if (posted === null) throw new Error('saveSettings() never POSTed');
    return JSON.parse(posted);
}

const [copyPath, benefitsArgument] = process.argv.slice(2);
const benefits = JSON.parse(benefitsArgument);
process.stdout.write(JSON.stringify(await run(copyPath, benefits)) + '\n');
