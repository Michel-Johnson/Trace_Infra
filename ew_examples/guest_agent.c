/* Guest vsock exec agent. PID 1 after /init mounts filesystems.
 *
 * Host connects to Firecracker's `{uds_path}_{port}` which forwards here.
 * Protocol, all integers network byte order:
 *   request:  uint32 cmd_len ; cmd bytes
 *   reply:    uint32 exitcode ; uint32 stdout_len ; stdout ; uint32 stderr_len ; stderr
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/vm_sockets.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define PORT 5252
#define MAX_CMD (256 * 1024)
#define MAX_OUT (1024 * 1024)

static int read_full(int fd, void *buf, size_t n) {
    unsigned char *p = buf;
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r == 0)
            return -1;
        if (r < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        got += (size_t)r;
    }
    return 0;
}

static int write_full(int fd, const void *buf, size_t n) {
    const unsigned char *p = buf;
    size_t put = 0;
    while (put < n) {
        ssize_t w = write(fd, p + put, n - put);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        put += (size_t)w;
    }
    return 0;
}

static uint32_t be32(uint32_t v) {
    return ((v & 0xff000000u) >> 24) | ((v & 0x00ff0000u) >> 8) |
           ((v & 0x0000ff00u) << 8) | ((v & 0x000000ffu) << 24);
}

static int run_cmd(const char *cmd, char **out, uint32_t *out_n,
                   char **err, uint32_t *err_n) {
    int outp[2], errp[2];
    if (pipe(outp) < 0 || pipe(errp) < 0)
        return 127;
    pid_t pid = fork();
    if (pid < 0)
        return 127;
    if (pid == 0) {
        dup2(outp[1], 1);
        dup2(errp[1], 2);
        close(outp[0]);
        close(outp[1]);
        close(errp[0]);
        close(errp[1]);
        execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
        _exit(127);
    }
    close(outp[1]);
    close(errp[1]);
    fcntl(outp[0], F_SETFL, O_NONBLOCK);
    fcntl(errp[0], F_SETFL, O_NONBLOCK);
    char *ob = malloc(MAX_OUT);
    char *eb = malloc(MAX_OUT);
    if (!ob || !eb)
        _exit(1);
    uint32_t on = 0, en = 0;
    int st = 0;
    int done = 0;
    while (!done) {
        pid_t w = waitpid(pid, &st, WNOHANG);
        if (w == pid)
            done = 1;
        char buf[4096];
        ssize_t r;
        while ((r = read(outp[0], buf, sizeof buf)) > 0) {
            uint32_t take = (uint32_t)r;
            if (on + take > MAX_OUT)
                take = MAX_OUT - on;
            memcpy(ob + on, buf, take);
            on += take;
        }
        while ((r = read(errp[0], buf, sizeof buf)) > 0) {
            uint32_t take = (uint32_t)r;
            if (en + take > MAX_OUT)
                take = MAX_OUT - en;
            memcpy(eb + en, buf, take);
            en += take;
        }
        if (!done)
            usleep(2000);
    }
    close(outp[0]);
    close(errp[0]);
    *out = ob;
    *out_n = on;
    *err = eb;
    *err_n = en;
    if (WIFEXITED(st))
        return WEXITSTATUS(st);
    if (WIFSIGNALED(st))
        return 128 + WTERMSIG(st);
    return 1;
}

static void handle(int fd) {
    uint32_t nbe;
    if (read_full(fd, &nbe, 4) < 0)
        return;
    uint32_t n = be32(nbe);
    if (n == 0 || n > MAX_CMD)
        return;
    char *cmd = malloc(n + 1);
    if (!cmd)
        return;
    if (read_full(fd, cmd, n) < 0) {
        free(cmd);
        return;
    }
    cmd[n] = 0;
    char *out = NULL, *err = NULL;
    uint32_t on = 0, en = 0;
    uint32_t code = (uint32_t)run_cmd(cmd, &out, &on, &err, &en);
    free(cmd);
    uint32_t cbe = be32(code), obe = be32(on), ebe = be32(en);
    write_full(fd, &cbe, 4);
    write_full(fd, &obe, 4);
    if (on)
        write_full(fd, out, on);
    write_full(fd, &ebe, 4);
    if (en)
        write_full(fd, err, en);
    free(out);
    free(err);
}

static void mounts(void) {
    mount("proc", "/proc", "proc", 0, NULL);
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mkdir("/tmp", 0777);
    mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
}

static int listen_vsock(void) {
    int lst = -1;
    for (int i = 0; i < 200 && lst < 0; i++) {
        lst = socket(AF_VSOCK, SOCK_STREAM, 0);
        if (lst < 0) {
            usleep(50000);
            continue;
        }
        struct sockaddr_vm addr;
        memset(&addr, 0, sizeof addr);
        addr.svm_family = AF_VSOCK;
        addr.svm_cid = VMADDR_CID_ANY;
        addr.svm_port = PORT;
        if (bind(lst, (struct sockaddr *)&addr, sizeof addr) < 0 ||
            listen(lst, 8) < 0) {
            close(lst);
            lst = -1;
            usleep(50000);
        }
    }
    return lst;
}

static int listen_unix(const char *path) {
    unlink(path);
    int lst = socket(AF_UNIX, SOCK_STREAM, 0);
    if (lst < 0)
        return -1;
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof addr.sun_path - 1);
    if (bind(lst, (struct sockaddr *)&addr, sizeof addr) < 0 || listen(lst, 8) < 0) {
        close(lst);
        return -1;
    }
    return lst;
}

static void serve(int lst) {
    for (;;) {
        int fd = accept(lst, NULL, NULL);
        if (fd < 0)
            continue;
        handle(fd);
        close(fd);
    }
}

int main(int argc, char **argv) {
    if (argc >= 3 && strcmp(argv[1], "--unix") == 0) {
        int lst = listen_unix(argv[2]);
        if (lst < 0)
            return 1;
        serve(lst);
        return 0;
    }
    mounts();
    int lst = listen_vsock();
    if (lst < 0)
        return 1;
    serve(lst);
    return 0;
}
