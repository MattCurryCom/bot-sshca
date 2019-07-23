import hashlib
import os
import subprocess
import time

import pytest

SUBTEAM = os.environ['SUBTEAM']
SUBTEAM_SECONDARY = os.environ['SUBTEAM_SECONDARY']
USERNAME = os.environ['KEYBASE_USERNAME']
BOT_USERNAME = os.environ['BOT_USERNAME']

# "uniquestring" is stored in /etc/unique of the SSH server. We then run the command `sha1sum /etc/unique` via kssh
# and assert that the output contains the sha1 hash of uniquestring. This checks to make sure the command given to
# kssh is actually executing on the remote server.
EXPECTED_HASH = hashlib.sha1(b"uniquestring").hexdigest().encode('utf-8')

def assert_contains_hash(output):
    assert EXPECTED_HASH in output

@pytest.fixture(autouse=True)
def run_around_tests():
    clear_keys()
    clear_local_config()
    # Calling yield triggers the test execution
    yield

def clear_keys():
    # Clear all keys generated by kssh
    try:
        run_command("rm -rf ~/.ssh/keybase-signed-key*")
    except subprocess.CalledProcessError:
        pass

def clear_local_config():
    # Clear kssh's local config file
    try:
        run_command("rm -rf ~/.ssh/kssh.config")
    except subprocess.CalledProcessError:
        pass

def simulate_two_teams(func):
    # A decorator that simulates running the given function in an environment with two teams set up
    def inner(*args, **kwargs):
        run_command(f"keybase fs read /keybase/team/{SUBTEAM}.ssh.staging/kssh-client.config | sed 's/{SUBTEAM}.ssh.staging/{SUBTEAM_SECONDARY}/g' | sed 's/{BOT_USERNAME}/otherbotname/g' | keybase fs write /keybase/team/{SUBTEAM_SECONDARY}/kssh-client.config")
        try:
            ret = func(*args, **kwargs)
        finally:
            run_command("keybase fs rm /keybase/team/%s/kssh-client.config" % SUBTEAM_SECONDARY)
        return ret
    return inner

def outputs_audit_log(expected_number):
    # A decorator that asserts that the given function triggers expected_number of audit logs to be added to '/keybase/team/team.ssh.staging/ca.log'
    # Note that fuse is not running in the container so this has to use `keybase fs read`
    def decorator(func):
        def inner(*args, **kwargs):
            cnt = 0

            # Make a set of the lines in the audit log before we ran
            before_lines = set(run_command("keybase fs read /keybase/team/%s.ssh.staging/ca.log" % SUBTEAM).splitlines())

            # Then run the function
            ret = func(*args, **kwargs)

            # And sleep for 1 second to give KBFS some time
            time.sleep(1)

            # Then see if there are new lines using set difference. This is only safe/reasonable since we include a
            # timestamp in audit log lines.
            after_lines = set(run_command("keybase fs read /keybase/team/%s.ssh.staging/ca.log" % SUBTEAM).splitlines())
            new_lines = after_lines - before_lines

            for line in new_lines:
                line = line.decode('utf-8')
                if line and "Processing SignatureRequest from user=%s" % USERNAME in line and "principals:staging,root_everywhere, expiration:+1h, pubkey:ssh-ed25519" in line:
                    cnt += 1

            if cnt != expected_number:
                assert False, "Found %s audit log entries, expected %s!" % (cnt, expected_number)
            return ret
        return inner
    return decorator

def run_command(cmd):
    return subprocess.check_output(cmd, shell=True)

@outputs_audit_log(expected_number=1)
def test_kssh_staging_user():
    # Test ksshing into staging as user
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no user@sshd-staging "sha1sum /etc/unique" """))

@outputs_audit_log(expected_number=1)
def test_kssh_staging_root():
    # Test ksshing into staging as user
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no root@sshd-staging "sha1sum /etc/unique" """))

@outputs_audit_log(expected_number=1)
def test_kssh_prod_root():
    # Test ksshing into prod as root
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /etc/unique" """))

@outputs_audit_log(expected_number=1)
def test_kssh_reject_prod_user():
    # Test that we can't kssh into prod as user since we aren't in the correct team for that
    try:
        run_command("""bin/kssh -o StrictHostKeyChecking=no user@sshd-prod "sha1sum /etc/unique" 2>&1 """)
        assert False
    except subprocess.CalledProcessError as e:
        assert b"Permission denied" in e.output
        assert EXPECTED_HASH not in e.output

@outputs_audit_log(expected_number=1)
def test_kssh_reuse():
    # Test that kssh reuses expired keys
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /etc/unique" """))
    start = time.time()
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /etc/unique" """))
    elapsed = time.time() - start
    assert elapsed < 0.75

@outputs_audit_log(expected_number=1)
def test_kssh_regenerate_expired_keys():
    # Test that kssh reprovisions a key when the stored keys are expired
    run_command("ls ~/")
    run_command("mv ~/tests/testFiles/expired ~/.ssh/keybase-signed-key-- && mv ~/tests/testFiles/expired.pub ~/.ssh/keybase-signed-key--.pub && mv ~/tests/testFiles/expired-cert.pub ~/.ssh/keybase-signed-key---cert.pub")
    assert_contains_hash(run_command("""bin/kssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /etc/unique" """))

@outputs_audit_log(expected_number=1)
def test_kssh_provision():
    # Test the `kssh --provision` flag
    # we have to run all of the below commands in one run_command call so that environment variables are shared
    # so ssh-agent can work
    output = run_command("""
    eval `ssh-agent -s`
    bin/kssh --provision
    ssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /etc/unique"
    echo -n foo > /tmp/foo
    scp /tmp/foo root@sshd-prod:/tmp/foo
    ssh -q -o StrictHostKeyChecking=no root@sshd-prod "sha1sum /tmp/foo"
    """)
    assert_contains_hash(output)
    assert hashlib.sha1(b"foo").hexdigest().encode('utf-8') in output

@outputs_audit_log(expected_number=0)
@simulate_two_teams
def test_kssh_errors_on_two_teams():
    # Test that kssh does not run if there are multiple teams, no client config, and no --team flag
    try:
        run_command("bin/kssh root@sshd-prod")
        assert False
    except subprocess.CalledProcessError as e:
        assert b"Found 2 config files" in e.output

@outputs_audit_log(expected_number=1)
@simulate_two_teams
def test_kssh_team_flag():
    # Test that kssh works with the --team flag
    assert_contains_hash(run_command("bin/kssh --team %s.ssh.staging -q -o StrictHostKeyChecking=no root@sshd-prod 'sha1sum /etc/unique'" % SUBTEAM))

@outputs_audit_log(expected_number=1)
@simulate_two_teams
def test_kssh_set_default_team():
    # Test that kssh works with the --set-default-team flag
    run_command("bin/kssh --set-default-team %s.ssh.staging" % SUBTEAM)
    assert_contains_hash(run_command("bin/kssh -q -o StrictHostKeyChecking=no root@sshd-prod 'sha1sum /etc/unique'"))

@outputs_audit_log(expected_number=1)
@simulate_two_teams
def test_kssh_override_default_team():
    # Test that the --team flag overrides the local config file
    run_command("bin/kssh --set-default-team %s" % SUBTEAM_SECONDARY)
    assert_contains_hash(run_command("bin/kssh --team %s.ssh.staging -q -o StrictHostKeyChecking=no root@sshd-prod 'sha1sum /etc/unique'" % SUBTEAM))

def pytest_sessionfinish(session, exitstatus):
    # Automatically run after all tests in order to ensure that no kssh-client config files stick around
    run_command("keybase fs rm /keybase/team/%s.ssh.staging/kssh-client.config || true" % SUBTEAM)
    run_command("keybase fs rm /keybase/team/%s.ssh.prod/kssh-client.config || true" % SUBTEAM)
    run_command("keybase fs rm /keybase/team/%s.ssh.root_everywhere/kssh-client.config || true" % SUBTEAM)
    run_command("keybase fs rm /keybase/team/%s/kssh-client.config || true" % SUBTEAM_SECONDARY)