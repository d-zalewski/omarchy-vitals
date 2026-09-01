# Contributing

The most useful contributions are **checks for hardware I can't test** — NVIDIA
and AMD graphics, wifi, bluetooth, laptop suspend, batteries, external displays.

Everything here runs without hardware, so you can write and test a check on any
machine and only need the real device to confirm it.

## Setup

There isn't one. Clone it and run the tests:

```bash
git clone https://github.com/d-zalewski/omarchy-vitals
cd omarchy-vitals
./run-tests.sh
```

Optionally add the coverage gate — one command, creates a local `.venv`:

```bash
./run-tests.sh --setup
```

## Adding a check, start to finish

**1. Scaffold it.** This writes the check *and* a test for every branch, so the
suite passes immediately and stays at 100 % coverage while you fill it in:

```bash
./new-check.py --module network --name wifi_regdomain \
               --desc "wireless regulatory domain is set" --requires iw --write
```

Leave off `--write` to print the stubs instead of appending them.
`./new-check.py --help` lists the modules and their tiers.

**2. Write the check.** Replace the `CHANGE_ME` parts. The generated stub
already has the shape a check should have:

```python
@check(tier=1, name="wifi_regdomain", desc="wireless regulatory domain is set",
       requires=["iw"])
def wifi_regdomain(ctx):
    r = ctx.run(["iw", "reg", "get"])
    if r.returncode != 0:
        return Skip("no wireless hardware")          # absent -> Skip
    if "country 00" in r.stdout:
        return Warn("regulatory domain unset - reduced channels and power")
    country = re.search(r"country (\w+)", r.stdout)
    return Ok(f"regulatory domain {country.group(1)}")
```

**3. Test every branch.** The scaffold gives you four tests; adjust them to
match. Nothing touches real hardware — `FakeContext` returns whatever you tell
it to:

```python
def test_no_wireless_skips(self):
    ctx = FakeContext(tools=["iw"], commands={"iw reg": cp("", 1)})
    self.assertIs(network.wifi_regdomain(ctx).status, Status.SKIP)

def test_unset_domain_warns(self):
    ctx = FakeContext(tools=["iw"], commands={"iw reg": cp("country 00: DFS-UNSET")})
    self.assertIs(network.wifi_regdomain(ctx).status, Status.WARN)

def test_domain_set(self):
    ctx = FakeContext(tools=["iw"], commands={"iw reg": cp("country GB: DFS-ETSI")})
    r = network.wifi_regdomain(ctx)
    self.assertIs(r.status, Status.PASS)
    self.assertIn("GB", r.message)
```

For checks that read `/sys` or `/proc`, use `fake_fs` instead — a string is a
file, a list is a directory, and nested globs work:

```python
with fake_fs({"/sys/class/power_supply": ["BAT0"],
              "/sys/class/power_supply/BAT0/capacity": "85"}):
    r = peripherals.battery(FakeContext())
```

**4. Run them:**

```bash
./run-tests.sh wifi          # just yours - filters by test name
./run-tests.sh               # everything, with the 100 % gate
```

**5. Try it on real hardware**, if you have it:

```bash
./omarchy-vitals.py --only wifi_regdomain
```

## The rules that matter

**Absent hardware returns `Skip`, never `Fail`.** A laptop without ethernet must
not fail an ethernet check. `SKIP` means "not applicable here"; `FAIL` means
"this machine has it and it's broken". Get this wrong and you build a suite
people learn to ignore.

**Silent degradation is a failure, not a warning.** If a GPU driver binds but
clients fall back to software rendering, everything still "works" — slowly, and
hot. That's worse than an obvious break, so it fails.

**Reports carry no identifying data.** No IP addresses, MAC addresses or
hostnames. Reports get committed and shared. Record that the gateway answered,
not its address.

**Metrics need a direction.** If your check emits a number that can get better
or worse, add it to `LOWER_IS_BETTER` or `HIGHER_IS_BETTER` in
`vitals/core.py`, or `compare` can't tell a regression from an improvement.

**Mark it `disruptive=True`** if it loads the machine, makes noise, or suspends
it — that's what `--skip-disruptive` excludes.

## Coverage is gated at 100 %

Not for its own sake. These checks run when something is *already* wrong, so the
failure and skip paths matter as much as the happy path — and those are exactly
the ones that go untested. The gate is what forces them.

It caught a real bug here: `snapshot()` in `stress.py` raised instead of
degrading when a sysfs directory was missing, which is precisely the situation
it exists to report on.

If a test fails after your change, check whether the *test* is wrong before
changing the code. Two failures during development were bad assertions, not
bugs.

## Pull requests

Small and focused is easier to review than complete. Say which hardware you
tested on — "works on my AMD 7900 XTX" is more useful than a clean CI run,
since CI has no GPU.

If you're adding a check for hardware nobody else here has, that's the most
valuable thing you can contribute, and imperfect is fine.
