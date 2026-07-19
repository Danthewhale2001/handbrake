/*
 * hb_preview — generate preview images using HandBrake's internal libhb
 * Finds libhb's temp preview dir and copies JPEGs before they're deleted
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/inotify.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include "handbrake/handbrake.h"

static char g_outdir[4096];
static int  g_nprev;
static volatile int g_done = 0;
static char g_hb_dir[512] = {0};
static pthread_mutex_t g_mutex = PTHREAD_MUTEX_INITIALIZER;

static void copy_file(const char *src, const char *dst)
{
    FILE *in  = fopen(src, "rb");
    FILE *out = fopen(dst, "wb");
    if (!in || !out) { if(in) fclose(in); if(out) fclose(out); return; }
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), in)) > 0)
        fwrite(buf, 1, n, out);
    fclose(in);
    fclose(out);
}

static int copy_all_from_dir(const char *hb_dir)
{
    DIR *d = opendir(hb_dir);
    if (!d) return 0;
    int copied = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        if (!strstr(e->d_name, ".jpg")) continue;
        /* filename format: 0_1_N.jpg where N is preview index */
        int idx = -1;
        char *last = strrchr(e->d_name, '_');
        if (last) idx = atoi(last+1);
        if (idx < 0 || idx >= g_nprev) continue;
        char src[1024], dst[1024];
        snprintf(src, sizeof(src), "%s/%s", hb_dir, e->d_name);
        snprintf(dst, sizeof(dst), "%s/preview_%03d.jpg", g_outdir, idx);
        /* Check source has real content (>50KB) */
        struct stat st;
        if (stat(src, &st) != 0 || st.st_size < 1000) continue;
        copy_file(src, dst);
        printf("%s\n", dst);
        fflush(stdout);
        copied++;
    }
    closedir(d);
    return copied;
}

/* Watch /tmp for handbrake- directory, record its path */
static void *watcher_thread(void *arg)
{
    int ifd = inotify_init();
    if (ifd < 0) return NULL;
    inotify_add_watch(ifd, "/tmp", IN_CREATE);

    /* Check for existing handbrake- dir first */
    DIR *d = opendir("/tmp");
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            if (strncmp(e->d_name, "handbrake-", 10) == 0) {
                pthread_mutex_lock(&g_mutex);
                snprintf(g_hb_dir, sizeof(g_hb_dir), "/tmp/%s", e->d_name);
                pthread_mutex_unlock(&g_mutex);
                break;
            }
        }
        closedir(d);
    }

    char buf[4096];
    while (!g_done) {
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(ifd, &fds);
        struct timeval tv = {0, 50000};
        if (select(ifd+1, &fds, NULL, NULL, &tv) <= 0) continue;
        int len = read(ifd, buf, sizeof(buf));
        if (len <= 0) continue;
        int i = 0;
        while (i < len) {
            struct inotify_event *ev = (struct inotify_event*)(buf + i);
            if (ev->len > 0 && strncmp(ev->name, "handbrake-", 10) == 0) {
                pthread_mutex_lock(&g_mutex);
                snprintf(g_hb_dir, sizeof(g_hb_dir), "/tmp/%s", ev->name);
                pthread_mutex_unlock(&g_mutex);
            }
            i += sizeof(struct inotify_event) + ev->len;
        }
    }
    close(ifd);
    return NULL;
}

int main(int argc, char *argv[])
{
    if (argc < 4) {
        fprintf(stderr, "Usage: hb_preview <input> <outdir> <num_previews>\n");
        return 1;
    }
    const char *input  = argv[1];
    const char *outdir = argv[2];
    int nprev = atoi(argv[3]);
    if (nprev < 1)  nprev = 1;
    if (nprev > 30) nprev = 30;

    mkdir(outdir, 0755);
    strncpy(g_outdir, outdir, sizeof(g_outdir)-1);
    g_nprev = nprev;

    if (hb_global_init_no_hardware() < 0) return 1;

    pthread_t wt;
    pthread_create(&wt, NULL, watcher_thread, NULL);
    usleep(100000); /* 100ms for watcher to set up */

    hb_handle_t *hb = hb_init(0);
    if (!hb) { g_done = 1; pthread_join(wt, NULL); return 1; }

    hb_list_t *paths = hb_list_init();
    hb_list_add(paths, (void*)input);
    hb_scan(hb, paths, 0, nprev, 1, 0, 0, 0, 0, NULL, 0, 0);
    hb_list_close(&paths);

    hb_state_t state;
    int ticks = 0;
    while (1) {
        hb_get_state(hb, &state);
        if (state.state == HB_STATE_SCANDONE) break;
        if (++ticks > 600) break;
        hb_snooze(100);
    }

    /* Copy ALL files from temp dir BEFORE hb_close deletes it */
    pthread_mutex_lock(&g_mutex);
    char hb_dir_copy[512];
    strncpy(hb_dir_copy, g_hb_dir, sizeof(hb_dir_copy));
    pthread_mutex_unlock(&g_mutex);

    int written = 0;
    if (hb_dir_copy[0]) {
        written = copy_all_from_dir(hb_dir_copy);
    }

    g_done = 1;
    pthread_join(wt, NULL);

    hb_close(&hb);
    hb_global_close();

    return written > 0 ? 0 : 1;
}
