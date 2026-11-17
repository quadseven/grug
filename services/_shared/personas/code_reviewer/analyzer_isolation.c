#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/landlock.h>
#include <seccomp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define READ_TREE (LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR)
#define EXEC_TREE (READ_TREE | LANDLOCK_ACCESS_FS_EXECUTE)
#define SCRATCH_TREE (READ_TREE | LANDLOCK_ACCESS_FS_WRITE_FILE | \
    LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG | \
    LANDLOCK_ACCESS_FS_MAKE_SYM | LANDLOCK_ACCESS_FS_REMOVE_DIR | \
    LANDLOCK_ACCESS_FS_REMOVE_FILE | LANDLOCK_ACCESS_FS_REFER | \
    LANDLOCK_ACCESS_FS_TRUNCATE)

static void fail(const char *operation)
{
    perror(operation);
    exit(2);
}

static void allow_path(int ruleset, const char *path, __u64 access, int optional)
{
    int fd = open(path, O_PATH | O_CLOEXEC);
    if (fd < 0) {
        if (optional && errno == ENOENT)
            return;
        fail(path);
    }
    struct stat info;
    if (fstat(fd, &info) < 0)
        fail(path);
    if (!S_ISDIR(info.st_mode))
        access &= LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_EXECUTE |
                  LANDLOCK_ACCESS_FS_WRITE_FILE;
    struct landlock_path_beneath_attr rule = {
        .allowed_access = access,
        .parent_fd = fd,
    };
    if (syscall(SYS_landlock_add_rule, ruleset, LANDLOCK_RULE_PATH_BENEATH, &rule, 0) < 0)
        fail(path);
    close(fd);
}

static void restrict_sockets(void)
{
    const int blocked[] = {
        SCMP_SYS(socket), SCMP_SYS(io_uring_setup), SCMP_SYS(io_uring_enter),
        SCMP_SYS(io_uring_register), SCMP_SYS(pidfd_getfd), SCMP_SYS(ptrace),
        SCMP_SYS(process_vm_readv), SCMP_SYS(process_vm_writev),
    };
    scmp_filter_ctx filter = seccomp_init(SCMP_ACT_ALLOW);
    if (!filter)
        fail("seccomp_init");
    for (size_t i = 0; i < sizeof(blocked) / sizeof(blocked[0]); i++) {
        if (seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), blocked[i], 0) < 0)
            goto error;
    }
    // asyncio needs private Unix stream pairs. Datagram pairs can send to
    // other Unix sockets, so deny them even when flag bits are present.
    if (seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(socketpair), 1,
                         SCMP_A0(SCMP_CMP_NE, AF_UNIX)) < 0 ||
        seccomp_rule_add(filter, SCMP_ACT_ERRNO(EPERM), SCMP_SYS(socketpair), 1,
                         SCMP_A1(SCMP_CMP_MASKED_EQ, 0xf, SOCK_DGRAM)) < 0 ||
        seccomp_load(filter) < 0)
        goto error;
    seccomp_release(filter);
    return;

error:
    fputs("could not install analyzer seccomp filter\n", stderr);
    exit(2);
}

int main(int argc, char **argv)
{
    if (argc < 5) {
        fputs("usage: analyzer-isolation SCAN SCRATCH RULES|- EXECUTABLE [args...]\n", stderr);
        return 2;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1L, 0L, 0L, 0L) < 0)
        fail("PR_SET_NO_NEW_PRIVS");

    // Handling TRUNCATE requires ABI 3. Older kernels fail closed here.
    const struct landlock_ruleset_attr policy = {
        .handled_access_fs = (LANDLOCK_ACCESS_FS_TRUNCATE << 1) - 1,
    };
    int ruleset = syscall(SYS_landlock_create_ruleset, &policy, sizeof(policy), 0);
    if (ruleset < 0)
        fail("landlock_create_ruleset");
    allow_path(ruleset, "/usr/lib", EXEC_TREE, 0);
    allow_path(ruleset, "/usr/local/lib", EXEC_TREE, 0);
    allow_path(ruleset, "/usr/local/bin", EXEC_TREE, 0);
    allow_path(ruleset, "/etc/ld.so.cache", READ_TREE, 1);
    allow_path(ruleset, "/opt/grug-opengrep", EXEC_TREE, 1);
    allow_path(ruleset, "/dev/null", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE, 0);
    allow_path(ruleset, "/dev/urandom", LANDLOCK_ACCESS_FS_READ_FILE, 0);
    allow_path(ruleset, argv[1], READ_TREE, 0);
    allow_path(ruleset, argv[2], SCRATCH_TREE, 0);
    if (strcmp(argv[3], "-") != 0)
        allow_path(ruleset, argv[3], READ_TREE, 0);
    allow_path(ruleset, argv[4], EXEC_TREE, 0);

    restrict_sockets();
    if (syscall(SYS_landlock_restrict_self, ruleset, 0) < 0)
        fail("landlock_restrict_self");
    close(ruleset);
    execv(argv[4], &argv[4]);
    fail("execv");
}
